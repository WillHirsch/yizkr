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
   to reference. A claim of a property the manifest marks
   `document_subject` (`issued_by`, `inception` and the others; ADR-0035
   §3.4, ADR-0039 §10.1) is written on its subject's item: its own source
   document's (an `artifact` subject naming the source); an artifact its
   source places, named by its media SHA-1, resolved as a source is; the
   item of a batch entity of an artifact kind (an un-held media item,
   such as a GEDCOM object the export dates); the item of an
   unclassified entity of its batch (a source's mention of an enclosure);
   or the existing item a curator confirmed either of those last two as
   (the claim's `confirmed_from`, the local subject the confirmation
   replaced).
   A claim whose subject is an artifact its source places, on a
   document-subject property or on the one property the manifest marks
   `placed_subject` (a `caption`, ADR-0039 §2.1), names an artifact among
   the media the caller gives as that source's placed media (`placed`),
   which must have an item. The pipeline gives the derived media it
   registers for the source in the same write; `yizkr ingest write` those
   its `--placed` names, from a container's derived-media record or by
   their SHA-1s. A document-subject claim whose subject is not its own
   source carries the passage that conveys it as its excerpt (ADR-0039
   §10.4). A subject of any other form, an artifact subject on a property
   the manifest marks neither way, a placed subject that names no
   artifact given as its source's in this write, or one no item carries,
   and a document-subject claim about another artifact with no excerpt
   are refused before the
   first write, as a claim whose text the instance cannot hold is
   (below). So is a claim of a property the
   manifest marks retired (`spouse`, which takes no new claim: a marriage
   is an entity, ADR-0036), or of a reference property (`stated_in`, a
   judgement note's), as its own property or a qualifier's: validation
   refuses both, and `yizkr write` takes a batch without validating it;
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
   yizkr.pipeline.registrar). A writer given `run_marks` states them of
   the run item in that second write too, each stated in the item: a
   scratch run marks its run items as an unratified scratch run's and
   records the hashes of its run manifest and identity map (ADR-0025
   §1). A dry run writes neither;
2. finds or makes the item of every unresolved `local` subject or value
   through the identity map the batch is written with, and through it alone
   (yizkr.pipeline.clustering, ADR-0035): each local id is a mention of the
   batch (yizkr.pipeline.fingerprints), every mention is in one cluster of
   the map, and each cluster is one item. Before anything is read or
   written, a batch that names a local entity is refused whole
   (UnmappedMention) when it is written with no map, or when the map does
   not hold every mention of the batch under an artifact its claims cite
   (mapped_clusters); an entity an approved equivalence names (ADR-0007)
   needs none, as its record names its item by QID. No item is ever found
   by its label alone, so a curator's decision that two entities are
   distinct, and the map's keeping two mentions apart, are never undone by
   a label they share. A cluster's item is made under its label (the label
   most of its mentions give, then the longest, of those the writer can
   send, `cluster_terms`), in the form the instance keeps labels in,
   whatever form the batch gives it in: trimmed, cleaned up, in Unicode
   NFC, and cut to the characters the instance holds in a label
   (`_term_text`), with a description made from the map alone
   (source_descriptor): the cluster's kind; the artifact kind of the
   document its mentions come from (the first of the artifact's `instance
   of` values, deprecated ones aside, that the vocabulary holds, read and
   never written), or how many documents they come from; and, from the
   second on, the cluster's place among the map's clusters of its label and
   kind, in the order of their cluster ids, so two people of one name,
   which Wikibase refuses as two items with one label and one description,
   take two descriptions, and a write of a batch with the same map, on the
   instance or after a reset, finds each cluster's item again: the earliest
   item under its label whose English description is its description
   (_cluster_item). Only the items under the label are read for it. The
   description is never a summary or a ref's description, which may give
   what the source asserts of the entity, such as its life dates, nor a
   title, which may name a living person, nor a QID or a digest. A minted
   item whose description would be its label gets none, with a warning, as
   Wikibase refuses an item whose label and description are the same. The
   keys hold within one map, so an instance holds the writes of one map: a
   run item's label gives the map's tag, and a batch written with another
   map than the instance's run items record is refused before anything is
   written (OtherIdentityMap), unless the caller allows maps to mix
   (`one_map`), as on an instance whose writers keep their labels apart; a
   scratch run resets the instance before it writes its one map (ADR-0025).
   The mentions are read from the batch the map was made from, when the
   caller gives it (`mapped_from`): the reviewed batch a write is given may
   have lost claims, which would move the mentions of a batch without an
   entity manifest. The instance's labels are read once a write, with the
   vocabulary known, and the run item is found among them and the items the
   write makes (`_FoundLabels`); a label two or more items hold names the
   numerically earliest of them (`ItemLabels`). Each item is made in one
   write, with its label, its description, its aliases, its `instance of`,
   and an unclassified entity's `type hint`, the last two with two
   references (_kind_references): stated in -> the artifact of the claim
   that mints it, and inferred from extraction run -> the run item, the
   run block of its `instance of` with the entity's judgement note, a kind
   or an identity call, where its record gives one (step 3); so a
   write that fails leaves no item, and none without its kind. An entity's
   kind is the extraction's proposal, a machine write like any claim
   (ADR-0003). A local ref resolves through the batch's entity-manifest
   record of its id when there is one (ADR-0007, read through
   `entity_records` in yizkr.pipeline.review, as the depicts gate reads
   it): the record gives the kind, type hint and aliases; otherwise the ref
   gives its own label and kind. A cluster's item is made with, or gains,
   append-only, as aliases the labels of all its mentions in the map and
   every label and alias the batch gives a local id of the cluster in a
   claim it writes; those of an entity an approved equivalence names go on
   before any claim is written. A cluster no mention of which has a label
   the instance keeps has no item: a ref to it raises ValueError, as one
   with no label does. A kind the vocabulary lacks is recorded for no item,
   minted or found: the result's `warnings` name each entity that gives
   one, found before the first write: a manifest record's kind whatever its
   claims, and the kind of a ref without a record only when a written claim
   mentions it. The label a cluster's item takes follows the side rule
   (ADR-0010): it takes its label when the item under the label is on its
   side (an artifact or a part of one, `is_artifact_item`: an item with a
   `media SHA-1` or a `media file`, or an `instance of` an artifact kind or
   of artifact fragment, for an artifact-kind cluster, of an artifact kind
   or of artifact fragment, `is_artifact_kind`, such as the GEDCOM import's
   media object, as long as registration did not make it (_registered): an
   artifact, which carries a `media SHA-1`, or its page fragment, `part of`
   one; an item that is neither for any other, but a manifest vocabulary
   item or a run item, whose label must name it alone, _process_item), or
   when no item holds the label. Otherwise it takes its tagged label
   (`tagged_label`): its label with MEDIA_OBJECT_TAG at its end for an
   artifact-kind cluster (`media_object_label`), and with ENTITY_TAG for
   any other, whatever its kind, its own label then an alias of the item.
   Where a media object does not take its label, the result's `flags` name,
   against the media object's item, as unhashed-title, every artifact
   registered under the label, whatever item holds it: the artifact under
   it (the fragment's whole, for a fragment) and every artifact under it
   with its own content tag in it ([sha1[:8]] of its media, or of the
   fragment's whole), at its end or before a fragment's ", page <n>"
   (_registered_under), in the order of their items, as the registrar flags
   each when the registration comes second (yizkr.pipeline.registrar), on
   every write that finds them so; a dry run names the item's placeholder.
   Where each cluster's item is found or made is settled before the first
   write (_plan), in a dry run too, with the labels the batch itself makes
   items under: the clusters not on the artifact side are placed first, so
   of a person and a media object of one batch under a label no item holds,
   the person takes it, whichever the batch names first. A cluster that may
   take neither its label nor its tagged label, as when registered
   artifacts hold both labels of a media object, is refused then, with
   every claim that names one of its local ids: the result's `refused`
   names those claims and a warning names each entity by its local id, its
   claims by their claim ids and the two items by QID, and the rest of the
   batch is written. For a refused artifact-kind entity the `flags` name,
   against the item under its tagged label, each artifact registered under
   its label; a refusal with no such pair, as of a person, place or other
   entity, or of a media object with no artifact registered under its
   label, is the warning and `refused` alone (ADR-0010). The plan is made
   of the claims the writer writes, without those refused before it
   (pre_plan_refusals: for their text, or as a misplaced document subject,
   ADR-0035 §3.4), and then without those whose item value the plan finds
   to be their subject's item (self_references, below), made again each
   time: an entity, such as a media object, that only such claims name is
   never placed, so it is neither blocked nor flagged. Of a manifest record
   that only refused claims name, refused before the plan, with an entity
   the plan blocks or as naming their subject's item, nothing is sent, so
   none of its aliases or type hint is named in the result's
   `refused_terms` (_unsent_records);
3. writes each claim as a statement at NORMAL rank (the contract has
   no rank field — ADR-0002) with TWO references (ADR-0003):
   * stated in -> artifact, with page number / printed page number /
     region / excerpt qualifiers inside the reference, the printed page
     number only where the claim's source gives one (the extractor reads
     it from the transcription's record, ADR-0037);
   * inferred from extraction run -> the run item, with the claim's
     judgement note, where it gives one, beside it in this run block
     (ADR-0035 §4): `judgement`, the item of the note's category, and
     `judgement_note`, its sentence, in NFC, on one line, cleaned up and
     cut to TEXT_LIMIT as a locator is (_locator_text), so a long note
     never refuses its claim. A note is never in the `stated in` block,
     never a qualifier, never on the run item. A note whose sentence holds
     nothing the instance keeps, or a character Python's Unicode tables do
     not know (below), is left out, with a warning, and its claim written
     without it (_sent_note). A curated claim has no run block, and so no
     note;
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
   * the statement carried that block before the write (_before_write):
     the claim adds no source block, and its run block only as the
     paragraph below says (safe re-run);
   * otherwise (a block the statement lacked before the write, including
     one another claim of the write has given it since, as on a
     statement the write made) the claim's blocks are APPENDED to the
     statement, each only if the statement does not already carry it, so
     a second claim of the same run adds its source block only where it
     cites another place, and the run's block only where its note
     differs. The same artifact under another page, region or excerpt
     thus adds a block of its own beside the first, as another artifact
     does. No block the statement carries is changed or removed.
   The run block is compared whole too, with its judgement note, in NFC
   (ADR-0035 §4). Where the statement carried the claim's source block
   before the write, the claim's run block is appended when no block of
   the statement carries it whole, and it carries a note or the
   statement cites its run in a block with another note. A curated
   statement, one with no extraction-run reference (a curator's own
   write, until a machine claim of this write or an earlier one joins
   it) or whose rank a curator changed, is compared by artifact alone:
   the claim is skipped when any of the statement's blocks cites the
   claim's artifact, under whatever locator, and otherwise its blocks
   are appended as above. Whether it was curated, the artifacts it cited
   and the blocks it carried are read as it stood before the write
   (_before_write), so whether a claim's artifact or place is recorded
   there is judged by the statement before the write, never by what
   another claim of the write gave it. It is compared as curated only
   while it still is one: a machine claim of the write joining a
   curator's own write at normal rank makes it a machine statement for
   every claim of the write, while one whose rank a curator changed
   stays curated. A claim that adds nothing is looked at again once the
   batch's other claims are written and its kinds recorded (step 5), so
   it meets the statement as the batch left it: it joins a block of its
   run that a noted claim after it, or a kind, gave the statement, and
   meets as a machine statement a curator's own write at normal rank
   that a machine claim after it joined, as it would had it come after
   them. So a second claim of the same run adds its own run block where
   its note differs, a run's noted and note-less claims, and a curator's
   claims beside machine claims, from one place or several, give the
   same blocks in whatever order the batch gives them, for the run that
   wrote the statement and for a later one, and a later run's note-less
   claims alone add nothing where their source agrees. One statement can
   thus carry several run blocks of one run; a reader counts runs by run
   item, never by blocks. A batch written again adds nothing. Same value
   but different qualifiers is a different assertion and coexists as its
   own statement (ADR-0002);
5. after the claims, records the kind every local ref of a written
   claim gives the item it resolves to, minted or found, and a record's
   type hint, each cited to the artifact of the claim the ref is in, so
   each such artifact is a witness of the kind: a ref's kind is its
   manifest record's when it has one, and its own `instance_of`
   otherwise. A later ref of an item the batch minted, under another
   local id of its cluster, as each chunk or page of a document can name
   an entity by an id of its own, witnesses its own kind so, and a
   second write of the batch adds nothing; the kind and type hint a
   minted item was made with are on it already. The kind and type hint
   of the manifest record an approved equivalence names are recorded on
   its item too, cited to the artifact of the first claim that mentions
   it (_kinds_given).
   A kind carries the judgement note of the manifest record that gives
   it, in its run block, as a claim's does (step 3), and a type hint
   none. A kind or type hint is compared with the item's unqualified
   statements on the same property (a type hint as free text, ignoring
   case, Unicode form and spacing), so a qualified statement never
   matches:
   * a matching statement already referenced to that artifact: nothing
     to add (safe re-run), but a run block that none of those statements
     carries whole, which a machine one of them gains where it carries a
     note or that statement cites its run already, as a claim's does
     (step 4); the witnesses whose note is written go first, so two
     mentions of an entity, each with its own note or none, give the
     same blocks in either order in a batch whose claims cite one
     artifact, as an extraction's and a GEDCOM export's do (in one that
     cites two, an approved equivalence's kind follows the first claim
     that mentions its item, and a minted item's kind the claim that
     mints it);
   * a matching machine statement at normal rank: it gains this
     source's two reference blocks, each only if not already there;
   * otherwise (a different value, or a same value held only by curated
     statements): a new statement at normal rank with both references,
     coexisting with the others (ADR-0002).
   A curated statement (one with no extraction-run reference, or whose
   rank a curator changed) is never altered. The claims come first so
   that an explicit `instance of` claim keeps its own locator and
   excerpt.

No statement has its own subject's item as its value. Validation refuses
a claim whose item value, its own or a qualifier's, names its subject
(ADR-0035 §3.5), but it reads the batch before the steps that put two
refs on one item: the identity map, which may put a claim's subject and
its value, two mentions, in one cluster, as clustering merges a father
and a son of one name with a `father` claim between them; an approved
equivalence, which gives the record of each its item's QID, as a
curator's correction of the equivalence sheet's QID can; and a QID a
ref carries. So the writer refuses, before the first write and in a dry
run too, every claim whose value or qualifier's value is, after those
steps, its subject's item (self_references): two refs are one item when
they name one QID, one vocabulary item, one artifact or one cluster of
the map, or when a cluster's item, as the plan finds it (_cluster_item),
is the item the other ref names. Such a claim is refused whole, as a
claim whose text the instance cannot hold is: nothing of it is written,
no item only it names is made, the result's `refused` names it and its
`self_references` names it again, apart from the other refusals, and a
warning says which of its values names the subject's item. The rest of
the batch is written. A claim refused already, for its text or with an
entity the plan blocks, is counted there and not read again, so each
refused claim is counted for one reason. The check reads the items as
the plan finds them, and is made again each time the plan is made again
without the claims it refuses, until it refuses no other.

Each refused claim is counted under the one reason that refused it, of
REFUSAL_REASONS, in the order the writer looks for them: a retired or
reference property, the rules of deaths and births on the person
(claims.vital_problems, ADR-0040), the subject rules, the claim's text,
an entity the side rule blocks, and an item value that is its subject's
item. The
result's `refusal_reasons` gives each claim of `refused` its reason, and
refused_by_reason counts them, every reason in that order, summing to
the claims refused.

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
  region or excerpt, holds one is refused whole, as above. An alias or
  a type hint that holds one is left out, never changed, and the
  result's `refused_terms` names it ("<local id>: <term>"), with a
  warning that names its characters by their code points. An entity's
  description is the writer's source descriptor, made of manifest keys
  alone, so none is named. An
  entity whose label
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

import functools
import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from yizkr.claims import (
    PLACED_FORM,
    SOURCE_FORM,
    ArtifactRef,
    Claim,
    ClaimBatch,
    CoordinateValue,
    EntityRecord,
    EntityRef,
    ExternalIdValue,
    ItemValue,
    JudgementNote,
    LocalRef,
    MonolingualValue,
    QidRef,
    QuantityValue,
    SourceReference,
    StringValue,
    TimeValue,
    UnknownValue,
    VocabRef,
    confirmed_kind,
    document_subject_form,
    has_excerpt,
    load_vocabulary,
    vital_problems,
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
    from yizkr.pipeline.clustering import IdentityMap
    from yizkr.pipeline.registrar import RegistrationFlag

# The most characters (Unicode code points) Wikibase holds in the text of a
# string, external-id or monolingual text value: its `string-limits` for
# the string and the monolingual text value types, an external id being a
# string value. It checks every snak of a statement it is sent, the value,
# each qualifier and each reference, after it has put a string value in
# NFC and cleaned it up (CLEANED_UP), and refuses the whole edit when one
# is longer.
TEXT_LIMIT = 400

# why the writer refuses a claim whole, in the order it looks for each
# (the module docstring): its property, or a qualifier's, is retired or a
# reference property (ADR-0036, ADR-0035 §4); its subject breaks the
# subject rules (ADR-0035 §3.4, ADR-0039 §2); a text of it is one the
# instance cannot hold as it is, its own, a locator's or a mentioned
# entity's label; it names an entity the side rule blocks (ADR-0010); an
# item value of it is its subject's item (ADR-0035 §3.5)
REFUSAL_REASONS = (
    "retired_or_reference_property",
    "vital_rule",
    "subject_rule",
    "text",
    "side_rule",
    "self_reference",
)
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
# how many hex digits of an identity map's hash a run item's label gives
# after its date (_run_terms), and the label a run item written with a map has
# (other_identity_maps)
MAP_TAG = 12
_RUN_LABEL_MAP = re.compile(r"^extraction run \S+ map ([0-9a-f]{12}) ")
# the content tag registration gives a label another item holds: " [", the
# first 8 hex digits of the media's SHA-1, "]" (yizkr.pipeline.registrar)
_CONTENT_TAG = re.compile(r" \[[0-9a-f]{8}\]")
# an item's QID, as a ref names it (self_references)
_QID = re.compile(r"Q[1-9][0-9]*")
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


def _snak_type(snak) -> str:
    """A snak's type as Wikibase names it: "value", "somevalue" (an
    unknown value) or "novalue"."""
    kind = getattr(snak, "snaktype", None)
    return getattr(kind, "value", kind) or "value"


@functools.lru_cache(maxsize=1)
def _datatypes() -> dict[str, str]:
    """Each manifest property's datatype, by key."""
    return load_vocabulary()[0]


def unsupported_dates(batch: ClaimBatch) -> list[str]:
    """Each date of `batch` the project does not support (ADR-0034), a
    claim's value or a qualifier's: the claim's id and property, which
    part holds it (its value, or the qualifier by its property) and the
    reason (``TimeValue.problem``). An unknown value has no date, so it
    has none."""
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


def other_identity_maps(
    labels: Iterable[str], identity_hash: str
) -> list[str]:
    """The MAP_TAG-digit tags of every identity map other than the one of
    `identity_hash` that the run items among the item `labels` of an
    instance record (_run_terms), sorted. Read from the labels alone."""
    return sorted(
        {
            found.group(1)
            for label in labels
            if (found := _RUN_LABEL_MAP.match(label))
        }
        - {identity_hash[:MAP_TAG]}
    )


class OtherIdentityMap(RuntimeError):
    """The instance holds writes of another identity map than the one a
    batch is written with: items are keyed within one map (step 2 of the
    module docstring)."""

    def __init__(self, tags: list[str]) -> None:
        self.tags = tags
        shown = ", ".join(tags)
        super().__init__(
            f"the instance holds the writes of another identity map (map "
            f"{shown}, by the run items' labels), and a cluster's item is "
            f"found again only by the map it was written with: write with "
            f"that map, or reset the instance (yizkr scratch reset) before "
            f"writing with this one. Nothing was written."
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
    # place, or its run's block alone (another note, or a bare block beside
    # a noted one of its run) (step 4 of the module docstring)
    referenced: int = 0
    created_items: dict[str, str] = field(
        default_factory=dict
    )  # local_id -> QID
    # the items this write made, never one it found: a subset of
    # created_items' values, in the order they were made
    minted: list[str] = field(default_factory=list)
    # step 5 of the module docstring, on the `instance of` and `type hint`
    # statements of items a ref or record found: the statements written,
    # and each artifact whose references an existing statement gained (a
    # statement two artifacts witness counts twice)
    type_statements_written: int = 0
    type_statements_referenced: int = 0
    # the run blocks with a judgement note this write added (ADR-0035 §4):
    # a claim's, on its statement, and an entity's, on the `instance of`
    # statement of its item (steps 3 to 5 of the module docstring); a dry
    # run adds none
    notes_written: int = 0
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
    # each label, alias or type hint of the batch's entities left out
    # because it holds a character Python's Unicode tables do not know
    # (unknown_characters), named "<local id>: <term>", in the order
    # write_batch finds them before its first write; a warning names each
    refused_terms: list[str] = field(default_factory=list)
    # the claim id of each claim of `refused` refused because an item
    # value of it, its own or a qualifier's, is its subject's item
    # (self_references), in batch order: counted apart from the other
    # refusals, and apart from extraction's rejects
    self_references: list[str] = field(default_factory=list)
    # the reason, of REFUSAL_REASONS, each claim of `refused` was refused
    # for, in the same order
    refusal_reasons: list[str] = field(default_factory=list)

    def refused_by_reason(self) -> dict[str, int]:
        """The claims refused, counted by reason: every reason of
        REFUSAL_REASONS, in its order, 0 for one no claim was refused for;
        the counts sum to the claims refused."""
        return {
            reason: self.refusal_reasons.count(reason)
            for reason in REFUSAL_REASONS
        }


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


def source_descriptor(
    kind: str | None,
    source: str | None,
    ordinal: int = 1,
    sources: int = 1,
) -> str:
    """The description a minted item takes: what the entity is and what
    names it, as a reader reads both, never anything the source asserts of
    the entity (its life dates among them), nor a source's title, which may
    name a living person, nor a QID or a digest: `kind`, the kind key of
    the entity's cluster with its underscores made spaces ("entity" without
    one), then "named in" and, for a cluster whose mentions come from one
    document, `source`, the artifact kind key of that document, made words
    the same way after "a" or "an" ("a source" without one), or, for one
    whose mentions come from `sources` documents, that many sources; then,
    for the `ordinal`-th cluster of its identity map under one label and of
    one kind from the second on (cluster_terms), that number in brackets.
    So two clusters under one label take two descriptions, as Wikibase
    refuses two items with one label and one description, and the
    description, made from the identity map alone, finds a cluster's item
    again (step 2 of the module docstring). It is in the form the instance
    keeps a term in (stored_term)."""
    what = (kind or "entity").replace("_", " ")
    suffix = f" ({ordinal})" if ordinal > 1 else ""
    if sources > 1:
        where = f"{sources} sources"
    elif source:
        words = source.replace("_", " ")
        article = "an" if words[:1].lower() in "aeiou" else "a"
        where = f"{article} {words}"
    else:
        where = "a source"
    return _term_text(f"{what} named in {where}{suffix}")


@dataclass(frozen=True)
class ClusterTerms:
    """What the writer makes of a cluster of an identity map
    (yizkr.pipeline.clustering.Cluster) before it reads the instance:
    `label`, the label its item is found or made under (before the side
    rule, side_label), in the form the writer sends a label (_term_text),
    or "" when no mention of it has a label the writer can send; `kind`,
    the cluster's kind key, or None; `aliases`, every other label of its
    mentions, as the writer sends an alias (_terms); `sources`, how many
    documents its mentions come from; and `ordinal`, its place from 1
    among the map's clusters of its label and kind (cluster_terms)."""

    label: str
    kind: str | None
    aliases: tuple[str, ...] = ()
    sources: int = 1
    ordinal: int = 1


def _cluster_label(labels: Collection[str]) -> str:
    """The label of a cluster whose mentions have the labels `labels` (in
    the order of their fingerprints, one per mention), as the writer sends
    a label (_term_text): of those it can send, which hold something the
    instance keeps and no character Python's Unicode tables do not know
    (unknown_characters), the one most of the mentions give, then the
    longest of those, the fullest form, then the first. "" when none."""
    sendable = [
        term
        for text in labels
        if not unknown_characters(text) and (term := _term_text(text))
    ]
    if not sendable:
        return ""
    counts: dict[str, int] = {}
    for term in sendable:
        counts[term] = counts.get(term, 0) + 1
    return max(
        dict.fromkeys(sendable),
        key=lambda term: (counts[term], len(term)),
    )


def cluster_terms(identity: IdentityMap) -> dict[str, ClusterTerms]:
    """Each cluster of `identity`, by its id, with what the writer makes of
    it (ClusterTerms). The clusters of one label and one kind are placed
    from 1 in the order of their cluster ids, so the places, and the
    descriptions made from them (source_descriptor), depend on the map
    alone: a write of a batch with the same map, on the instance or after
    a reset, gives each cluster the description it gave it, and finds its
    item again. Made once for each map (IdentityMap.memo)."""

    def compute() -> dict[str, ClusterTerms]:
        terms: dict[str, ClusterTerms] = {}
        places: dict[tuple[str, str], int] = {}
        for cid, cluster in sorted(identity.clusters().items()):
            label = _cluster_label(cluster.labels)
            kind = cluster.kind or None
            ordinal = 1
            if label:
                places[(label, cluster.kind)] = (
                    places.get((label, cluster.kind), 0) + 1
                )
                ordinal = places[(label, cluster.kind)]
            terms[cid] = ClusterTerms(
                label=label,
                kind=kind,
                aliases=tuple(
                    alias
                    for alias in _terms(list(cluster.labels))
                    if alias != label
                ),
                sources=max(len(cluster.documents), 1),
                ordinal=ordinal,
            )
        return terms

    return identity.memo("writer.cluster_terms", compute)


def media_object_label(label: str) -> str:
    """The label of the item for an artifact-kind entity, such as a GEDCOM
    media object, whose label `label` (as the writer sends a label,
    _term_text) a registered artifact holds: `label`, cut to leave room
    (_cut), then MEDIA_OBJECT_TAG. It depends on the label alone, so a
    re-run finds the item again, and the registrar looks for it under an
    artifact's title (ADR-0010)."""
    return _cut(label, TERM_LIMIT - len(MEDIA_OBJECT_TAG)) + MEDIA_OBJECT_TAG


def tagged_label(label: str, kind: str | None) -> str:
    """The label of the item for a cluster of the kind `kind` whose label
    `label` (as the writer sends a label, _term_text) an item holds whose
    label it may not take (step 2 of the module docstring): for a cluster
    on the artifact side (is_artifact_kind), `media_object_label`; for any
    other, of whatever kind or none, `label`, cut to leave room (_cut),
    then ENTITY_TAG. It depends on the label and the side alone, so a
    re-run finds the item again (ADR-0010)."""
    if is_artifact_kind(kind):
        return media_object_label(label)
    return _cut(label, TERM_LIMIT - len(ENTITY_TAG)) + ENTITY_TAG


def artifact_kinds() -> frozenset[str]:
    """The manifest keys of the artifact kinds: artifact, and each class
    the manifest makes a `subclass_of` it."""
    return load_acceptance().artifact_kinds


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


# what a local ref, or an approved equivalence's record, gives its item
# (step 5 of the module docstring): its kind, its type hint, the artifact
# QID of the claim it is in, and the entity's judgement note
_Witness = tuple[str | None, str | None, str, JudgementNote | None]


@dataclass(frozen=True)
class _Placement:
    """Where step 2 of the module docstring puts the item of a cluster of
    the identity map: `label`, the label its item is found or made under;
    `alias`, the cluster's own label, kept as an alias of that item when
    `label` is tagged (tagged_label); `held`, each registered artifact
    (artifact, SHA-1) flagged against its item
    (BatchWriter._registered_under); and `blocked`, for a cluster refused
    because it may take neither its label nor its tagged label, the QIDs of
    the items under those two, "" for an item the batch makes.
    `description` is the description its item is made with and found by
    (source_descriptor); `aliases`, the aliases its item is made with or
    gains: `alias`, the labels of the cluster's other mentions, and the
    labels and aliases the batch gives its mentions; `cluster`, its id."""

    label: str
    alias: str = ""
    held: tuple[tuple[str, str], ...] = ()
    blocked: tuple[str, str] | None = None
    description: str = ""
    aliases: tuple[str, ...] = ()
    cluster: str = ""


class _FoundLabels:
    """The items under each label on the instance, for one write: the
    instance's item labels read once, on first need
    (`WikiClient.label_index`), with the run item the write makes under
    its label since (`made`). A label two or more items hold (homonyms,
    such as two people of one name) names the numerically earliest of
    them, and keeps every holder (ItemLabels): the side rule reads the
    earliest (BatchWriter._joins), a cluster's item is found among the
    holders by its description (BatchWriter._cluster_item), and the run
    item's label must name one item alone (`sole`). The documents of a
    run are written one at a time, and the write itself makes every item
    it gives a label, so the labels read once stay current through the
    write: the plan (BatchWriter._plan), the artifacts registered under a
    label (BatchWriter._registered_under), the items found or made for the
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
    writer resolves. `records` is the batch's manifest (entity_records)."""
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


def subject_refusal(
    claim: Claim,
    placed: Mapping[str, Collection[str]],
    resolved: Collection[str] = (),
    subject_kind: str | None = None,
    confirmed: str | None = None,
) -> tuple[str, str] | None:
    """Why the writer refuses the claim's subject, with the decision that
    gives the rule, or None (the module docstring, step 0). A
    placed-subject property's subject is an artifact among the derived
    media `placed` names for the claim's source, by their SHA-1s, whose
    media SHA-1 an item carries (`resolved`, the SHA-1s resolved;
    ADR-0039 §2.1). A document-subject property's is one of the forms
    claims.document_subject_form names (`subject_kind` is the kind of a
    local subject, `confirmed` that of a QID subject's local subject
    before a curator's confirmation, claims.confirmed_kind):
    the claim's own source (ADR-0035 §3.4), an artifact `placed` names
    for it that an item carries, a batch entity of an artifact kind, an
    unclassified entity of the batch or the existing item a curator
    confirmed one of those two as; and a claim
    whose subject is not its own source carries the passage that conveys
    it as its excerpt, one that is not blank as the instance keeps text
    (_locator_text; ADR-0039 §10.1, §10.4). No other property takes an
    artifact subject (ADR-0039 §2)."""
    acceptance = load_acceptance()
    prop, subject = claim.property, claim.subject
    source = claim.source.artifact.sha1
    if prop in acceptance.placed_subjects:
        if not isinstance(subject, ArtifactRef):
            reason = "its property takes only an artifact its source places"
        elif subject.sha1 not in placed.get(source, ()):
            reason = (
                "its subject is no artifact given to this write as "
                "placed by its source"
            )
        elif subject.sha1 not in resolved:
            reason = "no item carries its subject's media SHA-1"
        else:
            return None
        return reason, "ADR-0039 §2.1"
    if prop in acceptance.document_subjects:
        form = document_subject_form(
            claim, acceptance, placed, subject_kind, confirmed
        )
        if form is None:
            if isinstance(subject, ArtifactRef):
                return (
                    "its subject is neither its own source nor an artifact "
                    "given to this write as placed by its source",
                    "ADR-0039 §10.1",
                )
            return (
                "its property takes only its source document, an artifact "
                "its source places, a batch entity of an artifact kind, "
                "an unclassified entity of its batch or the existing item "
                "a curator confirmed one of those two as",
                "ADR-0039 §10.1",
            )
        if form == PLACED_FORM and subject.sha1 not in resolved:
            return (
                "no item carries its subject's media SHA-1",
                "ADR-0039 §10.1",
            )
        excerpt = claim.source.transcription_excerpt
        if form != SOURCE_FORM and not (
            has_excerpt(claim) and _locator_text(excerpt.text)
        ):
            return (
                "its subject is not its own source, and it carries no "
                "excerpt of the passage that conveys it",
                "ADR-0039 §10.4",
            )
        return None
    if isinstance(subject, ArtifactRef):
        return (
            "its property does not take an artifact as its subject",
            "ADR-0035 §3.4",
        )
    return None


def pre_plan_refusals(
    batch: ClaimBatch,
    unnamed: Mapping[str, list[str]],
    placed: Mapping[str, Collection[str]] | None = None,
    resolved: Collection[str] = (),
) -> dict[int, tuple[str, str]]:
    """The claims of `batch` the writer refuses, by their place in the
    batch, each with its reason (REFUSAL_REASONS) and the warning that
    names it by its claim id and property and says why (the module
    docstring):

    * a claim of a property the manifest marks retired (`spouse`,
      ADR-0036), which takes no new claim, or of a reference property
      (`stated_in`, the judgement note's), which is written only in a
      statement's references (ADR-0035 §4), as the claim's property or a
      qualifier's: validation refuses both, and `yizkr write` takes a
      batch without validating it (`retired_or_reference_property`);
    * a claim the rules of deaths and births on the person refuse
      (claims.vital_problems, ADR-0040): a reference to a `birth` or
      `death` entity, which only a curator makes; an unknown value where
      it is not taken, with a bound, or with no excerpt; a death or birth
      property on a subject that is no person; `parentage` on a property
      that is no parent's or child's; a union `ended_by_death_of` someone
      the batch calls widowed or who is neither of its couple; a value
      naming a person the entity manifest leaves out (`vital_rule`);
    * a subject the subject rules refuse (subject_refusal, with `placed`,
      the artifacts given as each source's placed media, and
      `resolved`, the artifact SHA-1s an item carries): an artifact
      subject on a property that takes none, a caption whose subject is
      no artifact this write is given as placed by its source, a claim of a
      placed-subject property with any other subject, a claim of a
      document-subject property whose subject is of no form it takes, and
      one about an artifact other than its source with no excerpt
      (ADR-0035 §3.4, ADR-0039 §2, §10): the statement would go on the
      wrong item, or stand on no passage (`subject_rule`);
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

    The last three are each a `text` refusal.

    The warning quotes none of the texts."""
    acceptance = load_acceptance()
    kinds = {entity.id: entity.kind for entity in batch.entities}
    records = {entity.id: entity for entity in batch.entities}
    vital = vital_problems(batch, acceptance)
    refusals = {}
    for index, claim in enumerate(batch.claims):
        uses = [
            ("its property", claim.property),
            *(
                (f"its {qualifier.property} qualifier", qualifier.property)
                for qualifier in claim.qualifiers
            ),
        ]
        misused = [
            f"{where} is retired and takes no claim (ADR-0036)"
            if prop in acceptance.retired
            else f"{where} is a reference property, written only in a "
            f"statement's references (ADR-0035 §4)"
            for where, prop in uses
            if prop in acceptance.retired or prop in acceptance.references
        ]
        if misused:
            refusals[index] = (
                "retired_or_reference_property",
                f"claim {claim_id(claim)} ({claim.property}) is "
                f"refused, and nothing of it is written: "
                f"{'; '.join(misused)}",
            )
            continue
        if index in vital:
            refusals[index] = (
                "vital_rule",
                f"claim {claim_id(claim)} ({claim.property}) is refused, "
                f"and nothing of it is written: "
                f"{'; '.join(p.split(': ', 1)[-1] for p in vital[index])}",
            )
            continue
        subject = claim.subject
        refusal = subject_refusal(
            claim,
            placed or {},
            resolved,
            kinds.get(subject.local_id, subject.instance_of)
            if isinstance(subject, LocalRef)
            else None,
            confirmed_kind(claim, records),
        )
        if refusal is not None:
            reason, rule = refusal
            refusals[index] = (
                "subject_rule",
                f"claim {claim_id(claim)} ({claim.property}) is "
                f"refused, and nothing of it is written: {reason} ({rule})",
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
        stated = claim.circumstance_source
        if stated is not None:
            locators += [
                ("its circumstance's page number", stated.page_number),
                (
                    "its circumstance's printed page number",
                    stated.printed_page_number,
                ),
                (
                    "its circumstance's excerpt",
                    stated.transcription_excerpt.text
                    if stated.transcription_excerpt
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
                "text",
                f"claim {claim_id(claim)} ({claim.property}) is "
                f"refused, and nothing of it is written: "
                f"{'; '.join(faults)}. A cut or any other change would "
                f"alter what the source says",
            )
    return refusals


def self_references(
    batch: ClaimBatch,
    item_of: Callable[[EntityRef | ArtifactRef], str | None],
    skip: Collection[int] = (),
) -> dict[int, str]:
    """The claims of `batch` whose item value, the claim's own or a
    qualifier's, is the item its subject resolves to, by their place in
    the batch, each with the warning that names it by its claim id and
    property and says which of its values names that item (the module
    docstring). `item_of` gives the item a ref resolves to, as a QID or,
    for a cluster of the identity map whose item is not found, a key of
    the cluster; None when it cannot tell, which matches nothing. The
    claims at the places `skip`, refused already, are not read."""
    found = {}
    for index, claim in enumerate(batch.claims):
        if index in skip:
            continue
        subject = item_of(claim.subject)
        if subject is None:
            continue
        values = [("its value", claim.value)]
        values.extend(
            (f"its {qualifier.property} qualifier", qualifier.value)
            for qualifier in claim.qualifiers
        )
        named = [
            where
            for where, value in values
            if isinstance(value, ItemValue) and item_of(value.value) == subject
        ]
        if named:
            shown = (
                subject
                if _QID.fullmatch(subject)
                else "its cluster's item in the identity map"
            )
            found[index] = (
                f"claim {claim_id(claim)} ({claim.property}) is refused, "
                f"and nothing of it is written: {' and '.join(named)} "
                f"{'names' if len(named) == 1 else 'name'} its subject's "
                f"item ({shown}), and no statement has its own subject as "
                f"its value (ADR-0035 §3.5)"
            )
    return found


def side_label(
    label: str, kind: str | None, joins: Callable[[str], bool]
) -> tuple[str, bool]:
    """The label the item of a cluster with the label `label` (as the
    writer sends a label, _term_text) and the kind `kind` is found or made
    under (step 2 of the module docstring), and whether the cluster may
    take it: `label` when it may (`joins`), and otherwise its tagged label
    (tagged_label), which it takes when it may take that one, and is
    refused at otherwise. `joins` holds the side rule (ADR-0010): the
    label of which item the cluster may take, and which label nobody
    holds it may make one under."""
    if joins(label):
        return label, True
    tagged = tagged_label(label, kind)
    return tagged, joins(tagged)


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


def _sent_note(note: JudgementNote | None) -> str:
    """A judgement note's sentence as the writer sends it in its run
    block (_locator_text), or "" when it sends no note: none is given, or
    the sentence holds nothing the instance keeps, or a character Python's
    Unicode tables do not know (unknown_characters), which the instance
    may keep in another form, so that a re-run would add the block again.
    A note left out never refuses its claim or its entity's kind (ADR-0035
    §4); write_batch warns of each before its first write."""
    if note is None or unknown_characters(note.text):
        return ""
    return _locator_text(note.text)


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


def _run_terms(batch: ClaimBatch, identity_hash: str = "") -> tuple[str, str]:
    """The label and description of the batch's run item, as the writer
    looks for and writes them. The description records the hash of the
    identity map the batch is written with, when there is one
    (`identity_hash`), and the label gives "map" and its first MAP_TAG hex
    digits after the run's date, before the model identifier, which a cut
    may take off the label's end: so a write of the batch with another map
    has a run item of its own, and a write finds the maps an instance was
    written with in its labels alone (other_identity_maps). Both are in
    the form the instance keeps a term in, cut with a digest of the whole
    when a long model identifier takes the label past what the instance
    holds, so each batch's run item keeps a label of its own (_term_text).
    Raises ValueError, naming the characters, when either holds a character
    Python's Unicode tables do not know (unknown_characters), whole, before
    any cut: the instance may keep it in another form, and a re-run would
    mint the run item again. Both are machine text, from the run's own
    settings, so the batch is refused before anything is read or written
    rather than its run left unlabelled."""
    run = batch.extraction_run
    tag = f"map {identity_hash[:MAP_TAG]} " if identity_hash else ""
    label = (
        f"extraction run {run.started_at.date().isoformat()} {tag}"
        f"{run.model_identifier} {batch_fingerprint(batch)}"
    )
    description = (
        f"pipeline {run.pipeline_version}"
        + (f", commit {run.software_commit}" if run.software_commit else "")
        + f", started {run.started_at.isoformat()}"
        + (f", identity map {identity_hash}" if identity_hash else "")
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


def mapped_clusters(
    batch: ClaimBatch,
    identity: IdentityMap | None,
    mapped_from: ClaimBatch | None = None,
) -> tuple[dict[str, str], str | None, dict[str, ClusterTerms]]:
    """Each local id of `batch`, by the id of its mention's cluster in
    `identity`; the document the batch's mentions are mapped under; and the
    map's clusters' terms (cluster_terms). The mentions are read from
    `mapped_from`, the batch the map was made from, when it is given (the
    extraction's batch, of which `batch` is the reviewed and resolved
    form), and from `batch` otherwise. Nothing for a batch that names
    no local entity by a local ref, but those an approved equivalence
    names, whose item it is (ADR-0007): it needs no map. Raises
    UnmappedMention for a batch that names one when `identity` is None, or
    does not hold every mention of the batch under any artifact its claims
    cite
    (IdentityMap.clusters_of): the identity map is the only way a local
    entity finds or mints its item (step 2 of the module docstring)."""
    from yizkr.pipeline.clustering import UnmappedMention

    records = entity_records(batch)
    named = list(
        dict.fromkeys(
            ref.local_id
            for claim in batch.claims
            for ref in local_refs(claim)
            # an approved equivalence names its entity's item (ADR-0007)
            if not (ref.local_id in records and records[ref.local_id].qid)
        )
    )
    if not named:
        return {}, None, {}
    if identity is None:
        raise UnmappedMention(
            named, "the batch is written with no identity map"
        )
    # the mentions are those of the batch the map was made from: a review
    # that drops claims changes the stand-in label and the order of the
    # mentions of a batch without an entity manifest, never theirs
    source = mapped_from or batch
    cited = Counter(claim.source.artifact.sha1 for claim in source.claims)
    # the artifact most of its claims cite first: the document's own
    documents = sorted(cited, key=lambda sha1: -cited[sha1])
    document, clusters = identity.clusters_of(source, documents)
    # a local ref the mapped batch does not name, or that a batch with an
    # entity manifest has no record of, is no mention of it
    unrecorded = [local_id for local_id in named if local_id not in clusters]
    if unrecorded:
        raise UnmappedMention(
            unrecorded,
            "the batch the map was made from has no mention of them",
        )
    return clusters, document, cluster_terms(identity)


def check_batch(
    batch: ClaimBatch,
    identity: IdentityMap | None,
    mapped_from: ClaimBatch | None = None,
) -> tuple[dict[str, str], str | None, dict[str, ClusterTerms], str]:
    """What write_batch checks of `batch` before it reads from or writes
    to the instance, in its order, raising as it does: a depicts claim
    without its row approval (UnapprovedDepictsError), an unsupported date
    (UnsupportedDate), a mention the map does not hold (UnmappedMention,
    mapped_clusters), and a run item's term the instance may keep in
    another form (ValueError, _run_terms). Reads the batch and the map
    alone, so a caller can refuse the batch with it before anything of
    its document is registered. Returns what mapped_clusters returns,
    and the hash of the map the batch is written with ("" for a batch
    that names no local entity)."""
    unapproved = unapproved_depicts(batch)
    if unapproved:
        raise UnapprovedDepictsError(unapproved)
    unsupported = unsupported_dates(batch)
    if unsupported:
        raise UnsupportedDate(
            "the batch holds unsupported dates, and nothing was "
            "written: " + "; ".join(unsupported)
        )
    # the batch's mentions, each in its cluster of the identity map,
    # before anything is read or written: an unmapped one refuses the
    # batch whole
    clusters, document, terms = mapped_clusters(batch, identity, mapped_from)
    identity_hash = identity.hash if identity and clusters else ""
    # the run item's terms are machine text: one the instance may keep in
    # another form refuses the batch before anything is read or written
    _run_terms(batch, identity_hash)
    return clusters, document, terms, identity_hash


class BatchWriter:
    def __init__(
        self,
        client: WikiClient,
        dry_run: bool = False,
        artifact_qids: Mapping[str, str] | None = None,
        run_marks: Mapping[str, str] | None = None,
    ) -> None:
        """`artifact_qids` maps media SHA-1 to artifact QID for artifacts
        the caller has just registered. A cited SHA-1 it does not hold is
        looked up in the instance's `media SHA-1` index. `run_marks` gives
        string statements, by property key, that each run item the writer
        makes or finds states of itself (step 1 of the module
        docstring)."""
        self.client = client
        self.dry_run = dry_run
        self.run_marks = dict(run_marks or {})
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
        # claim that mentions the item; item QID -> each witness (kind,
        # type hint, artifact QID, judgement note) a local ref resolved to
        # it gives, on the artifact of the claim the ref is in; and the one
        # a minted item was made with
        self._mentions: dict[str, str] = {}
        self._witnesses: dict[str, list[_Witness]] = {}
        self._minted_with: dict[str, _Witness] = {}
        # per write_batch call: local id -> the id of its mention's cluster
        # in the identity map, the document the batch's mentions are mapped
        # under, and each cluster's terms (cluster_terms)
        self._clusters: dict[str, str] = {}
        self._document: str | None = None
        # per write_batch call: the hash of the identity map the batch is
        # written with, "" for a batch that names no local entity
        self._identity_hash = ""
        self._terms: dict[str, ClusterTerms] = {}
        # per write_batch call: local id -> where step 2 puts its item,
        # settled before the first write (_plan)
        self._placements: dict[str, _Placement] = {}
        # per write_batch call: the local ids of the manifest records of
        # which nothing is sent (_unsent_records)
        self._unsent: set[str] = set()
        # per write_batch call: the item each label names, the instance's
        # labels read once (_FoundLabels)
        self._labels = _FoundLabels(client)
        # per write_batch call: (label, description) -> the item of a
        # cluster, found or made by the write (_cluster_item), and the
        # clusters whose found item has been given its aliases
        self._items: dict[tuple[str, str], str] = {}
        self._aliased: set[str] = set()
        # per write_batch call: how each statement a claim matches stood
        # before the write (_before_write)
        self._before: dict[
            tuple, tuple[bool, frozenset[str], frozenset] | None
        ] = {}

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

    def resolve_placed(
        self, batch: ClaimBatch, placed: Mapping[str, Collection[str]]
    ) -> set[str]:
        """The SHA-1s of the artifacts the batch's claims take as placed
        subjects, among those `placed` names for each claim's source, that
        an item carries, each resolved as a source is (resolve_artifacts),
        the index read afresh when one is not known. Reads only, and
        raises nothing: a placed subject no item carries is refused with
        its claim (pre_plan_refusals)."""
        wanted = sorted(
            {
                claim.subject.sha1
                for claim in batch.claims
                if isinstance(claim.subject, ArtifactRef)
                and claim.subject.sha1 != claim.source.artifact.sha1
                and claim.subject.sha1
                in placed.get(claim.source.artifact.sha1, ())
            }
        )
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
        return {sha1 for sha1 in wanted if sha1 in self._artifact_qids}

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
            # a claim's own source document, or an artifact its source
            # places, resolved in step 0
            return self._artifact_qids[ref.sha1]
        if isinstance(ref, QidRef):
            qid = ref.qid
        elif isinstance(ref, LocalRef):
            qid = self._materialize_local(ref, result, source_artifact_qid)
            if source_artifact_qid and not self.dry_run:
                # each ref gives its item its kind, and a record's type hint,
                # on the artifact of the claim it is in, whether the item is
                # minted or found (step 5 of the module docstring)
                record = self._entity_records.get(ref.local_id)
                witness = (
                    record.kind if record else ref.instance_of,
                    record.type_hint if record else None,
                    source_artifact_qid,
                    record.note if record else None,
                )
                witnesses = self._witnesses.setdefault(qid, [])
                if witness not in witnesses:
                    witnesses.append(witness)
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
        if record and record.qid:
            # reviewed equivalence: attach to the existing item and
            # contribute this document's aliases to it
            self._ensure_aliases(record.qid, [record.label, *record.aliases])
            result.created_items[ref.local_id] = record.qid
            return record.qid
        # the item of the ref's cluster in the identity map, found or made
        # under the label and description settled before the first write
        # (_plan); a cluster no mention of which has a label the instance
        # keeps has none
        placement = self._placements.get(ref.local_id)
        if placement is None:
            raise ValueError(
                f"local ref {ref.local_id!r} has no label, or none but "
                f"whitespace; cannot create an item for it (resolve it to "
                f"a QID or add a label)"
            )
        label = placement.label
        description = placement.description
        existing = self._cluster_item(placement)
        if existing:
            self._flag_held(placement, existing, result)
            if placement.cluster not in self._aliased:
                # the cluster's names, once a write, append-only
                self._aliased.add(placement.cluster)
                self._ensure_aliases(existing, list(placement.aliases))
            result.created_items[ref.local_id] = existing
            return existing
        # Wikibase refuses an item whose label and description are the
        # same: such a description is left out, and a warning says so, in a
        # dry run too
        if description == label:
            description = ""
            result.warnings.append(
                f"entity {ref.local_id!r} has a description that is its "
                f"label: its item is made without one, as Wikibase refuses "
                f"an item whose label and description are the same"
            )
        instance_of = record.kind if record else ref.instance_of
        if self.dry_run:
            # the pair a write would flag is reported against the item's
            # placeholder, which every mention of the cluster takes
            placeholder = f"(new:{ref.local_id})"
            self._items[(label, description)] = placeholder
            result.created_items[ref.local_id] = placeholder
            self._flag_held(placement, placeholder, result)
            return placeholder
        # The item is made whole in one write: its label, description and
        # aliases, its kind and an unclassified entity's type hint, so a
        # write that fails leaves no item, and none without its kind. The
        # kind and type hint come from the extraction, never from review,
        # so they carry the run reference even when the claim that
        # mentions the entity is a curated one (ADR-0003).
        item = self.client.wbi.item.new()
        item.labels.set("en", label)
        if description:
            item.descriptions.set("en", description)
        names = [
            alias
            for alias in _terms(list(placement.aliases))
            if alias != label
        ]
        if names:
            item.aliases.set("en", names)
        # the entity's judgement note, a kind or an identity call, goes in
        # the run block of its `instance of` (ADR-0035 §4)
        note = record.note if record else None
        noted = False
        if instance_of in self.items and source_artifact_qid:
            claim = self._item_claim(
                self.properties["instance_of"], self.items[instance_of]
            )
            claim.references = self._kind_references(
                source_artifact_qid, result.run_qid, note
            )
            noted = any(map(self._noted, claim.references.references))
            self._append(item, claim)
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
            hint.references = self._kind_references(
                source_artifact_qid, result.run_qid
            )
            self._append(item, hint)
        item = item.write(summary="ingest: entity proposed by extraction")
        qid = item.id
        result.notes_written += noted
        self._items[(label, description)] = qid
        if source_artifact_qid:
            self._minted_with[qid] = (
                instance_of,
                record.type_hint if record else None,
                source_artifact_qid,
                note,
            )
        self._aliased.add(placement.cluster)
        self._flag_held(placement, qid, result)
        result.created_items[ref.local_id] = qid
        result.minted.append(qid)
        return qid

    def _item_of(self, ref: EntityRef | ArtifactRef) -> str | None:
        """The item `ref` resolves to, before the first write, for
        self_references: the QID it names, of a vocabulary item, an
        artifact (resolved in step 0) or an existing item; the QID an
        approved equivalence gives its record; the item the plan finds for
        its cluster (_placements, _cluster_item), so it meets a ref naming
        that item by its QID; and otherwise the key of its cluster in the
        identity map, whose item the write makes. Reads, never writes."""
        if isinstance(ref, VocabRef):
            return self.items.get(ref.key, f"vocabulary {ref.key}")
        if isinstance(ref, QidRef):
            return ref.qid
        if isinstance(ref, ArtifactRef):
            return self._artifact_qids.get(ref.sha1)
        if not isinstance(ref, LocalRef):
            return None
        record = self._entity_records.get(ref.local_id)
        if record and record.qid:
            return record.qid
        placement = self._placements.get(ref.local_id)
        if placement is not None:
            found = self._cluster_item(placement)
            if found:
                return found
        cid = self._clusters.get(ref.local_id)
        return f"cluster {cid}" if cid else None

    def _kind_references(
        self,
        artifact_qid: str,
        run_qid: str | None,
        note: JudgementNote | None = None,
    ):
        """The references of a kind or type hint an item is made with or
        gains (step 5 of the module docstring): stated in the artifact of
        the claim whose ref gives it, and inferred from the run, with the
        entity's judgement note on its kind, if any, in the run block
        (_references). The one place an entity's kind is referenced."""
        return self._references(
            artifact_qid=artifact_qid, run_qid=run_qid, note=note
        )

    def _cluster_item(self, placement: _Placement) -> str | None:
        """The item of a cluster placed so (_Placement): the item the write
        found or made for it, or the earliest of the items under its label
        whose English description is its description (one that is the
        label goes without one, as it is made), or None, when the writer
        makes it one. Only the items under its label are read (_entity), so
        no item is found by its label alone, and no item of another cluster
        under the label is ever taken (step 2 of the module docstring)."""
        description = (
            ""
            if placement.description == placement.label
            else placement.description
        )
        key = (placement.label, description)
        if key in self._items:
            return self._items[key]
        for qid in self._labels.items().holders(placement.label):
            held = str(self._entity(qid).descriptions.get("en") or "")
            if held == description:
                self._items[key] = qid
                return qid
        return None

    def _artifact_kind(self, qid: str) -> str | None:
        """The artifact kind of the artifact `qid`, as a source descriptor
        names it (source_descriptor): the manifest key of the first of its
        `instance of` values, deprecated ones aside, that the vocabulary
        holds, or None. Read, never written (_entity)."""
        keys = {item: key for key, item in self.items.items()}
        statements = self._entity(qid).claims.get(
            self.properties["instance_of"]
        )
        for statement in statements:
            if statement.rank.value == "deprecated":
                continue
            value = (statement.mainsnak.datavalue or {}).get("value") or {}
            key = keys.get(value.get("id"))
            if key:
                return key
        return None

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
        """May a cluster, on the artifact side (is_artifact_kind) when
        `artifact`, take `label`, as the instance holds its items' labels
        (`labels`) and the batch makes items under the labels `planned` (label
        -> whether a cluster on the artifact side makes it)? The side rule of
        ADR-0010 reads the earliest item under the label; the cluster's item is
        then found there by its description, or made beside it, never that
        item for its label alone (_cluster_item). A label nobody holds is free.
        A cluster on the artifact side may take the label of an item
        registration did not make (_registered) that is an artifact or a part
        of one (_artifact_item): an artifact registered before content
        hashing, another media object's item, or a fragment of an artifact
        with no hash. Any other cluster may take the label of an item that is
        neither. Each may take a label the batch makes an item under for a
        cluster of its own side. No cluster takes the label of a vocabulary
        item or a run item (_process_item)."""
        qid = labels.get(label)
        if qid is None:
            return planned.get(label, artifact) == artifact
        if self._process_item(qid):
            return False
        if artifact:
            return self._registered(qid) is None and self._artifact_item(qid)
        return not self._artifact_item(qid)

    def _process_item(self, qid: str) -> bool:
        """Is the item `qid` one whose label must name it alone: an item of
        the manifest's vocabulary, or a run item (an `instance of`
        extraction run)? No cluster takes such a label, so a cluster under
        a vocabulary item's label, as "marriage", takes its tagged label
        and the vocabulary keeps its label to itself (ItemLabels.sole)."""
        if qid in set(self.items.values()):
            return True
        run = self.items.get("extraction_run")
        if run is None:
            return False
        claims = self._entity(qid).get_json().get("claims") or {}
        values = (
            ((statement.get("mainsnak") or {}).get("datavalue") or {}).get(
                "value"
            )
            for statement in claims.get(self.properties["instance_of"], [])
        )
        return any(
            isinstance(value, dict) and value.get("id") == run
            for value in values
        )

    def _place(
        self,
        label: str,
        kind: str | None,
        labels: _FoundLabels,
        planned: dict[str, bool],
    ) -> _Placement:
        """Where the item of a cluster with the label `label` and the kind
        `kind` is found or made (_Placement): under its label when it may
        take it (_joins), and otherwise under its tagged label
        (tagged_label), when it may take that one; and when neither, it is
        blocked (side_label). An entity on the artifact side
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
        """Where step 2 puts the item of each local entity of `batch`
        (_place), for the claims the writer writes, all but those at the
        places `refused`: each local id named in them by a local ref is
        placed with its mention's cluster in the identity map, under the
        cluster's label and kind (cluster_terms), and every local id of one
        cluster with one placement. An entity an approved equivalence names
        has none, nor has a cluster with no label the instance keeps. Every
        cluster not on the artifact side (is_artifact_kind) is placed
        first, then every cluster on it, each in the order its first ref
        comes in, so that of two clusters of the batch under one label that
        no item holds, the person, place or other entity takes it and the
        artifact-kind one its tagged label. `labels` finds the items under
        the labels, before the first write.

        Each placement carries the description its item is made with and
        found by (source_descriptor): the cluster's kind, the artifact kind
        of the document its mentions come from (_artifact_kind), or how
        many documents they come from, and the cluster's place among the
        map's clusters of its label and kind; and the aliases its item is
        made with or gains: the labels of the cluster's other mentions, and
        each label and alias the batch gives a local id of the cluster in a
        claim the writer writes, its manifest record's or its refs'."""
        members: dict[str, list[str]] = {}
        names: dict[str, list[str]] = {}
        for index, claim in enumerate(batch.claims):
            if index in refused:
                continue
            for ref in local_refs(claim):
                record = self._entity_records.get(ref.local_id)
                if record and record.qid:
                    continue
                cid = self._clusters[ref.local_id]
                if not self._terms[cid].label:
                    continue
                ids = members.setdefault(cid, [])
                if ref.local_id not in ids:
                    ids.append(ref.local_id)
                    if record:
                        names.setdefault(cid, []).extend(
                            [record.label, *record.aliases]
                        )
                if record is None and ref.label:
                    names.setdefault(cid, []).append(ref.label)
        planned: dict[str, bool] = {}
        placed: dict[str, _Placement] = {}
        for artifact in (False, True):
            for cid in members:
                terms = self._terms[cid]
                if is_artifact_kind(terms.kind) == artifact:
                    placed[cid] = self._place(
                        terms.label, terms.kind, labels, planned
                    )
        source = None
        if any(self._terms[cid].sources == 1 for cid in placed):
            source = self._artifact_kind(
                self._artifact_qids[self._document or ""]
            )
        placements: dict[str, _Placement] = {}
        for cid, placement in placed.items():
            terms = self._terms[cid]
            aliases = [placement.alias] if placement.alias else []
            aliases += [*terms.aliases, *names.get(cid, [])]
            placement = replace(
                placement,
                description=source_descriptor(
                    terms.kind,
                    source if terms.sources == 1 else None,
                    terms.ordinal,
                    terms.sources,
                ),
                aliases=tuple(
                    alias
                    for alias in dict.fromkeys(aliases)
                    if _term_text(alias) != placement.label
                ),
                cluster=cid,
            )
            for local_id in members[cid]:
                placements[local_id] = placement
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

        An unknown value is always added as a statement of its own:
        WikibaseIntegrator finds two statements equal by their values and
        qualifiers alone, so to it an unknown value (ADR-0040 §2.2) equals
        a curator's "no value" of the same property, neither having a
        value, and APPEND_OR_REPLACE would replace that statement. The
        writer has already compared snak types (_find_matching_statement)
        before it adds one.
        """
        from wikibaseintegrator.wbi_enums import ActionIfExists

        entity.claims.add(
            claim,
            action_if_exists=ActionIfExists.FORCE_APPEND
            if beside_equal or _snak_type(claim.mainsnak) != "value"
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
        if isinstance(value, UnknownValue):
            # Wikibase's "some value": the source states one, not which
            # (ADR-0040 §2.2); validation lets only an item or a time
            # property (burial_place, date_of_death) take it
            from wikibaseintegrator.wbi_enums import WikibaseSnakType

            unknown = {"time": Time, "wikibase-item": Item}.get(
                _datatypes().get(prop_key)
            )
            if unknown is None:
                raise TypeError(
                    f"property {prop_key!r} takes no unknown value"
                )
            return unknown(
                prop_nr=pid, snaktype=WikibaseSnakType.UNKNOWN_VALUE
            )
        raise TypeError(f"unhandled value {value!r}")

    def _references(
        self,
        artifact_qid: str,
        run_qid: str | None = None,
        claim: Claim | None = None,
        note: JudgementNote | None = None,
    ):
        """A statement's reference blocks (ADR-0003): its source block,
        `stated in` the artifact with the claim's locators and excerpt; its
        run block, `inferred from extraction run` the run item with the
        judgement note `note`, if any (_note_snaks); and, for a claim whose
        sourcing circumstance its source states of its own making
        (`Claim.circumstance_source`, ADR-0008), a second source block,
        `stated in` the same artifact with the locators and excerpt of that
        statement. A dry run's placeholder run item gives no run block."""
        from wikibaseintegrator.datatypes import Item
        from wikibaseintegrator.models import Reference, References

        references = References()
        references.add(
            self._source_block(
                artifact_qid, claim.source if claim is not None else None
            )
        )
        # dry-run mints no run item; its placeholder cannot be referenced
        if run_qid is not None and re.fullmatch(r"Q[1-9]\d*", run_qid):
            run_ref = Reference()
            run_ref.add(
                Item(
                    prop_nr=self.properties["inferred_from_extraction_run"],
                    value=run_qid,
                )
            )
            for snak in self._note_snaks(note):
                run_ref.add(snak)
            references.add(run_ref)
        if claim is not None and claim.circumstance_source is not None:
            references.add(
                self._source_block(artifact_qid, claim.circumstance_source)
            )
        return references

    def _source_block(
        self, artifact_qid: str, source: SourceReference | None = None
    ):
        """A statement's source block (ADR-0003): `stated in` the artifact
        with the page number, printed page number, region and excerpt
        `source` gives, if any."""
        from wikibaseintegrator.datatypes import Item, MonolingualText, String
        from wikibaseintegrator.models import Reference

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
        return source_ref

    def _note_snaks(self, note: JudgementNote | None) -> list:
        """The snaks of a judgement note in its run block (ADR-0035 §4):
        `judgement`, the item of its category, and `judgement_note`, its
        sentence as the writer sends a locator (_locator_text: in NFC, on
        one line, cleaned up, and cut to TEXT_LIMIT with the digest of the
        whole). None for no note, for one the writer leaves out
        (_sent_note), and for one whose terms the vocabulary lacks
        (_note_terms_missing)."""
        from wikibaseintegrator.datatypes import Item, String

        text = _sent_note(note)
        if not text or self._note_terms_missing(note):
            return []
        return [
            Item(
                prop_nr=self.properties["judgement"],
                value=self.items[f"judgement_{note.category}"],
            ),
            String(prop_nr=self.properties["judgement_note"], value=text),
        ]

    def _note_terms_missing(self, note: JudgementNote) -> list[str]:
        """The keys a judgement note is written with that the instance's
        vocabulary lacks: `judgement`, `judgement_note` and the item of
        its category. A note with one is left out, as a kind the
        vocabulary lacks is (write_batch warns once before its first
        write)."""
        return [
            key
            for key, terms in (
                ("judgement", self.properties),
                ("judgement_note", self.properties),
                (f"judgement_{note.category}", self.items),
            )
            if key not in terms
        ]

    # -- extraction-run item -------------------------------------------------

    def _other_maps(self) -> list[str]:
        """The tags of the identity maps other than the batch's that the
        instance's run items record (other_identity_maps)."""
        return other_identity_maps(self._labels.items(), self._identity_hash)

    def ensure_run_item(
        self, batch: ClaimBatch, identity_hash: str = ""
    ) -> str:
        label, description = _run_terms(batch, identity_hash)
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
                marks=self.run_marks,
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
            marks=self.run_marks,
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

        Equal means same property, same snak type and mainsnak value, AND
        the same qualifiers. An unknown value ("some value") and a "no
        value" carry no datavalue, so only the snak type tells them apart:
        one never matches the other, or a dated statement. Equal needs the
        same qualifiers: a same-value claim with different qualifiers (a
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
        new_type = _snak_type(wbi_claim.mainsnak)
        new_value = (wbi_claim.mainsnak.datavalue or {}).get("value")
        new_qualifiers = self._qualifier_snapshot(wbi_claim)
        for existing in entity.claims.get(prop) or []:
            if _snak_type(existing.mainsnak) != new_type:
                continue
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

    def _blocks_to_add(
        self,
        statement,
        references,
        artifact_qid: str,
        before: tuple[bool, frozenset[str], frozenset] | None = None,
    ) -> list:
        """The reference blocks of a claim that the equal statement it
        matches gains (step 4 of the module docstring); none when the
        claim is already recorded there.

        `references` are the claim's blocks as `_references` builds them,
        its source block first, then its run block, if any, then the block
        of the statement its sourcing circumstance stands on, if any. Each is
        compared whole with the statement's blocks: the source block's
        `stated in` with the page number, printed page number, region and
        excerpt, and the run block's `inferred from extraction run` with
        its judgement note (ADR-0035 §4). So the same artifact under
        another locator or excerpt, another place where it asserts the
        value, adds a block of its own, and so does a note of the run that
        no block of the statement carries. The blocks returned are those
        the statement does not already carry, so a run's block is not
        repeated for a second claim of the same run with the same note, or
        none. Where the statement carried the claim's source block before
        the write (`before`, from _before_write), its run block is added
        only when it carries a note or the statement cites its run in
        another block (one with another note): a later run without a note
        adds nothing to a block its source agrees with. A statement that
        was curated before the write, and still is, is compared by artifact
        alone, by the artifacts it cited then: one of them that is the
        claim's records the claim, and otherwise the claim adds each of
        its blocks the statement does not carry now. So a curated claim
        citing an artifact, or a source block the write added, changes
        nothing another claim of the write adds; a machine claim joining
        a curator's own write at normal rank does, making it a machine
        statement, and write_batch's re-check of the claims that added
        nothing makes the order irrelevant.
        """
        present = {
            self._reference_key(block)
            for block in statement.references.references
        }
        source, *runs = references.references
        curated, cited, carried = before or (False, frozenset(), frozenset())
        # a curator's own write at normal rank that a machine claim of the
        # write has joined since is compared as a machine one; one whose
        # rank a curator changed stays curated
        curated = curated and self._is_curated(statement)
        if curated:
            if artifact_qid in cited:
                return []
        elif self._reference_key(source) in carried:
            run = self.properties["inferred_from_extraction_run"]
            return [
                block
                for block in runs
                if self._reference_key(block) not in present
                and (
                    not block.snaks.get(run)
                    or self._noted(block)
                    or self._cites_run(statement, block)
                )
            ]
        return [
            block
            for block in references.references
            if self._reference_key(block) not in present
        ]

    def _before_write(self, subject_qid: str, wbi_claim, statement):
        """How the statement equal to `wbi_claim` on `subject_qid` stood
        before this write: whether it was curated (_is_curated), the
        artifacts its blocks cited, and its blocks (_reference_key). Kept
        the first time a claim of the write looks for it, before anything
        of the write changes it. None when no statement matched at the
        claim's first look; a statement the write minted an item with is
        read as minted, which is never curated."""
        key = (
            subject_qid,
            wbi_claim.mainsnak.property_number,
            json.dumps(
                self._value_key(wbi_claim.mainsnak.datavalue.get("value")),
                sort_keys=True,
            ),
            json.dumps(self._qualifier_snapshot(wbi_claim)),
        )
        if key not in self._before:
            stated_in = self.properties["stated_in"]
            self._before[key] = (
                None
                if statement is None
                else (
                    self._is_curated(statement),
                    frozenset(
                        ((snak.datavalue or {}).get("value") or {}).get("id")
                        for block in statement.references.references
                        for snak in block.snaks.get(stated_in) or []
                    ),
                    frozenset(
                        self._reference_key(block)
                        for block in statement.references.references
                    ),
                )
            )
        return self._before[key]

    def _noted(self, block) -> bool:
        """Does the reference block carry a judgement note? None does on
        an instance whose vocabulary has no `judgement`."""
        judgement = self.properties.get("judgement")
        return bool(judgement and block.snaks.get(judgement))

    def _cites_run(self, statement, run_block) -> bool:
        """Does any of the statement's reference blocks cite the run item
        that `run_block` cites?"""
        prop = self.properties["inferred_from_extraction_run"]
        runs = {
            ((snak.datavalue or {}).get("value") or {}).get("id")
            for snak in run_block.snaks.get(prop) or []
        }
        return any(
            ((snak.datavalue or {}).get("value") or {}).get("id") in runs
            for block in statement.references.references
            for snak in block.snaks.get(prop) or []
        )

    # -- the kinds the refs and records give their items ---------------------

    def _kinds_given(self) -> dict[str, list[_Witness]]:
        """Each item a claim of the batch mentions, with each witness (kind,
        type hint, artifact QID, judgement note) the batch gives it (step 5
        of the module docstring): every local ref resolved to it, on the
        artifact of the
        claim the ref is in (_resolve_ref), and every manifest record an
        approved equivalence names, on the artifact of the first claim that
        mentions its item, but one of which nothing is sent
        (_unsent_records). Of a minted item, the one it was made with is
        left out."""
        given = {
            qid: list(witnesses) for qid, witnesses in self._witnesses.items()
        }
        for record in self._entity_records.values():
            if (
                record.qid
                and record.qid in self._mentions
                and record.id not in self._unsent
            ):
                witness = (
                    record.kind,
                    record.type_hint,
                    self._mentions[record.qid],
                    record.note,
                )
                witnesses = given.setdefault(record.qid, [])
                if witness not in witnesses:
                    witnesses.append(witness)
        return {
            qid: kept
            for qid, witnesses in given.items()
            if (
                kept := [
                    witness
                    for witness in witnesses
                    if witness != self._minted_with.get(qid)
                ]
            )
        }

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
        self,
        qid: str,
        claim,
        artifact_qid: str,
        result: WriteResult,
        note: JudgementNote | None = None,
    ) -> None:
        """Record that `artifact_qid` gives the item `claim`'s kind or
        type hint, a kind with its entity's judgement note `note` in its
        run block (step 5 of the module docstring)."""
        entity = self._entity(qid)
        value = self._type_value(claim)
        same = [
            statement
            for statement in entity.claims.get(claim.mainsnak.property_number)
            or []
            if self._type_value(statement) == value
            and not self._qualifier_snapshot(statement)
        ]
        references = self._kind_references(artifact_qid, result.run_qid, note)
        cited = [
            s for s in same if self._has_artifact_reference(s, artifact_qid)
        ]
        if cited:
            # the artifact witnesses the kind already: a run block none of
            # those statements carries whole joins the machine one where it
            # carries a note, or that statement cites its run already, as a
            # claim's does (step 4 of the module docstring), and nothing
            # else is added
            target = next((s for s in cited if not self._is_curated(s)), None)
            carried = {
                self._reference_key(reference)
                for s in cited
                for reference in s.references.references
            }
            added = [
                block
                for block in references.references[1:]
                if target is not None
                and self._reference_key(block) not in carried
                and (self._noted(block) or self._cites_run(target, block))
            ]
            if not added:
                return
            for block in added:
                target.references.add(block)
            entity.write(summary="ingest: run block on a type statement")
            result.notes_written += sum(map(self._noted, added))
            self._entity_cache[qid] = self.client.wbi.item.get(qid)
            return
        target = next((s for s in same if not self._is_curated(s)), None)
        if target is not None:
            present = {
                self._reference_key(reference)
                for reference in target.references.references
            }
            added = [
                reference
                for reference in references.references
                if self._reference_key(reference) not in present
            ]
            for reference in added:
                target.references.add(reference)
            entity.write(
                summary="ingest: additional source for existing type statement"
            )
            result.type_statements_referenced += 1
            result.notes_written += sum(map(self._noted, added))
        else:
            claim.references = references
            self._append(entity, claim, beside_equal=True)
            entity.write(summary="ingest: type statement from another source")
            result.type_statements_written += 1
            result.notes_written += sum(
                map(self._noted, references.references)
            )
        self._entity_cache[qid] = self.client.wbi.item.get(qid)

    def _record_kinds(self, result: WriteResult) -> None:
        from wikibaseintegrator.datatypes import String

        for qid, witnesses in self._kinds_given().items():
            # the witnesses whose note is written go first, so a bare one of
            # the same run that an artifact already gave the kind finds its
            # run's noted block, in whatever order the batch gave them (the
            # run block rule of step 4 of the module docstring)
            for kind, type_hint, artifact_qid, note in sorted(
                witnesses, key=lambda witness: not self._note_snaks(witness[3])
            ):
                # a kind the vocabulary lacks is left out, with the warning
                # write_batch gave before its first write, and so is the
                # entity's note, which goes on its `instance of` alone
                if kind in self.items:
                    self._record_type_statement(
                        qid,
                        self._item_claim(
                            self.properties["instance_of"], self.items[kind]
                        ),
                        artifact_qid,
                        result,
                        note,
                    )
                # and so is a type hint the instance cannot hold, or may
                # keep in another form; one it holds goes with its spacing
                # made single spaces and cleaned up as the instance gives it
                # back (_hint_sent)
                hint = _hint_sent(type_hint or "")
                if hint:
                    self._record_type_statement(
                        qid,
                        String(
                            prop_nr=self.properties["type_hint"], value=hint
                        ),
                        artifact_qid,
                        result,
                    )

    def _unknown_terms(
        self, unnamed: Mapping[str, list[str]]
    ) -> tuple[list[str], list[str]]:
        """The terms of the batch's entities the writer leaves out because
        they hold a character Python's Unicode tables do not know
        (unknown_characters), which the instance may keep in another form:
        each named "<local id>: <term>" (label, alias or type hint), and a
        warning for each that names the entity and the characters, quoting
        no text. `unnamed` holds the entities whose label keeps the writer
        from finding or minting their item (unnamed_entities): their label
        is named, and nothing else of them, as nothing else of them is
        sent. An approved equivalence's record gives its label as an alias
        of its item. No description is named: the one a minted item takes
        is the writer's own (source_descriptor), made of manifest keys
        alone. A record's aliases and
        type hint are named whether or not a claim the writer writes
        mentions it, as the writer may send them to an item the resolve
        step matched the record to, but for a record of which nothing is
        sent (_unsent_records): the claims that name it by a local ref
        are all refused, before the plan (pre_plan_refusals), with an
        entity the plan blocks, or as naming their subject's item
        (self_references)."""
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
        for record in self._entity_records.values():
            if record.id in unnamed:
                continue
            if record.qid:
                leave_out(record.id, "label", record.label)
            if record.id in self._unsent:
                continue
            for alias in record.aliases:
                leave_out(record.id, "alias", alias)
            leave_out(record.id, "type hint", record.type_hint)
        return named, warnings

    def _unsent_records(
        self, batch: ClaimBatch, refused: Collection[int]
    ) -> set[str]:
        """The local ids of the manifest records without an approved
        equivalence that claims of `batch` name by a local ref, but only claims
        at the places `refused`, which the writer refuses, before the plan
        (pre_plan_refusals: for their text, or as a misplaced document
        subject), with an entity the plan blocks (step 2 of the module
        docstring), or as naming their subject's item (self_references): no
        item is found or made for such a record, and none of its terms, kind or
        type hint is sent anywhere (_kinds_given). A record counts as written
        only through a local ref to it in a claim the writer writes: one that
        claim names by the QID an approved equivalence gave it does not
        count."""
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

    def _notes_left_out(
        self, batch: ClaimBatch, refused: Collection[int]
    ) -> list[str]:
        """A warning for each judgement note the writer leaves out
        (_sent_note), quoting none of it: a claim's, of a claim it writes
        with a run block (not refused, not curated), and an entity's, of a
        record of which something is sent (_unsent_records). Its claim is
        written, and its entity's kind recorded, without it (ADR-0035
        §4); the kind of a record no written claim mentions is not
        recorded at all, and its warning says so. One warning more names
        the note terms the vocabulary lacks, if any, and how many notes
        are left out for them (_note_terms_missing)."""

        def why(note: JudgementNote) -> str:
            unknown = unknown_characters(note.text)
            if unknown:
                return _unknown_fault(unknown)
            return "holds nothing the instance keeps"

        written = [
            claim
            for index, claim in enumerate(batch.claims)
            if index not in refused
        ]
        named = {
            ref.local_id for claim in written for ref in local_refs(claim)
        }
        cited = {
            ref.qid
            for claim in written
            for ref in (
                claim.subject,
                *(
                    value.value
                    for value in (
                        claim.value,
                        *(q.value for q in claim.qualifiers),
                    )
                    if isinstance(value, ItemValue)
                ),
            )
            if isinstance(ref, QidRef)
        }
        notes = [
            (
                f"claim {claim_id(claim)} ({claim.property})",
                "the claim is written without it",
                claim.note,
            )
            for claim in written
            if not claim.curated and claim.note is not None
        ]
        notes += [
            (
                f"entity {record.id!r}",
                "its kind is recorded without it"
                if record.id in named or (record.qid and record.qid in cited)
                else "it is not recorded",
                record.note,
            )
            for record in self._entity_records.values()
            if record.id not in self._unsent and record.note is not None
        ]
        warnings = [
            f"{whose} has a judgement note that {why(note)}: {outcome}"
            for whose, outcome, note in notes
            if not _sent_note(note)
        ]
        missing = [
            (note, self._note_terms_missing(note))
            for _, _, note in notes
            if _sent_note(note) and self._note_terms_missing(note)
        ]
        if missing:
            keys = dict.fromkeys(key for _, lacks in missing for key in lacks)
            warnings.append(
                f"the vocabulary lacks {_listed(list(keys))}: "
                + (
                    "1 judgement note is left out, and its claim or kind is "
                    "written without it"
                    if len(missing) == 1
                    else f"{len(missing)} judgement notes are left out, and "
                    f"their claims and kinds are written without them"
                )
            )
        return warnings

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
        self,
        batch: ClaimBatch,
        bulk_accepted: bool = False,
        identity: IdentityMap | None = None,
        mapped_from: ClaimBatch | None = None,
        one_map: bool = True,
        claim_summary: str | None = None,
        placed: Mapping[str, Collection[str]] | None = None,
    ) -> WriteResult:
        """Write `batch`, finding or minting the item of each of its local
        entities through the identity map `identity` alone (step 2 of the
        module docstring). Raises UnapprovedDepictsError, before reading
        from or writing to the instance, when a depicts claim in it lacks
        its row approval (the module docstring), in a dry run too,
        UnsupportedDate, then too, when a claim holds an unsupported date
        (unsupported_dates), ValueError, then too, when the run item's
        label or description holds a character Python's Unicode tables do
        not know (_run_terms), and UnmappedMention, then too, when the batch
        names a local entity and `identity` is None or does not hold every
        mention of the batch (yizkr.pipeline.clustering.IdentityMap), read
        from `mapped_from`, the batch the map was made from, when it is
        given (mapped_clusters). With `one_map`, it raises OtherIdentityMap
        before anything is written, in a dry run too, when a run item on the
        instance records another map than `identity` (_other_maps): an
        instance holds the writes of one map. Without it, as on an instance
        whose writers keep their labels apart, as the live tests' session
        marks do, maps may mix. A
        claim whose own text the instance cannot hold raises nothing: it is
        refused alone, and the rest of the batch is written (the module
        docstring); so is a claim whose item value, its own or a
        qualifier's, is its subject's item (self_references), and an entity
        that may take neither its label nor its tagged label, refused with
        the claims that name it (step 2 of the module docstring).
        `claim_summary` is the edit summary of each
        claim's write, by default one that says whether its batch was
        reviewed row by row or accepted whole. `placed` names, by the SHA-1
        of a source's media, the SHA-1s of the artifacts it places: the
        derived media registered for that source in this write, or those
        a container's derived-media record names (`yizkr ingest write
        --placed`), which a `caption` claim of that source may take as its
        subject (step 0 of the module docstring). Without it a caption is
        refused."""
        self._clusters, self._document, self._terms, self._identity_hash = (
            check_batch(batch, identity, mapped_from)
        )
        # a bulk-accepted batch (whole-sheet accept-all, ADR-0023) gets a
        # distinct edit summary so MediaWiki history is the low-scrutiny
        # reappraisal queue — no graph flag, no staleness.
        if claim_summary is None:
            claim_summary = (
                "ingest: write bulk-accepted claim"
                if bulk_accepted
                else "ingest: write reviewed claim"
            )
        # every cited artifact must resolve before the first write; an
        # artifact a source places resolves where an item carries it, and
        # a caption about one that does not is refused below
        self.resolve_artifacts(batch)
        resolved = self.resolve_placed(batch, placed or {})
        # the manifest as the depicts gate read it (ADR-0005): one lookup
        self._entity_records = entity_records(batch)
        self._mentions = {}
        self._witnesses = {}
        self._minted_with = {}
        # the instance's labels, read once for the write, on first need,
        # and its items read afresh: an item read for an earlier write may
        # have been hashed by a registration since (_registered)
        self._labels = _FoundLabels(self.client)
        self._items = {}
        self._aliased = set()
        self._entity_cache = {}
        self._before = {}
        self._placements = {}
        # one map an instance: refused before anything is written
        if one_map and self._identity_hash:
            others = self._other_maps()
            if others:
                raise OtherIdentityMap(others)
        # found before the first write, in a dry run as well: the claims
        # whose own text the instance cannot hold, which the loop below
        # leaves out whole
        unnamed = unnamed_entities(batch, self._entity_records)
        planned = pre_plan_refusals(batch, unnamed, placed, resolved)
        refusals = {index: warning for index, (_, warning) in planned.items()}
        reasons = {index: reason for index, (reason, _) in planned.items()}
        # settled before the first write too: where each entity's item is
        # found or made, and the entities that may join neither the item
        # under their label nor the one under their tagged label, refused
        # with the claims that name them (step 2 of the module docstring);
        # then the claims whose item value is their subject's item, as the
        # plan finds the items, the plan made again without them until it
        # finds no other
        loops: dict[int, str] = {}
        while True:
            stopped, blocked, flags = self._blocked(batch, refusals)
            found = self_references(
                batch, self._item_of, skip={*refusals, *stopped}
            )
            if not found:
                break
            loops.update(found)
            refusals.update(found)
        refused = {*refusals, *stopped}
        reasons.update(dict.fromkeys(stopped, "side_rule"))
        reasons.update(dict.fromkeys(loops, "self_reference"))
        # the manifest records that only refused claims name, of which
        # nothing is sent
        self._unsent = self._unsent_records(batch, refused)
        # found before the first write too: the kinds the vocabulary lacks
        # (a manifest record's whatever its claims, and the kind of a ref
        # without a record only when a claim the writer writes mentions it:
        # all but those refused before the plan, those the plan refuses
        # and those naming their subject's item), the type hints the
        # instance cannot hold, and the terms it may keep in another form,
        # each skipped wherever the writer would record it
        warnings = self._unknown_kinds(batch, refused)
        warnings += [
            *refusals.values(),
            *blocked,
            *self._type_hints_over_limit(),
            *self._notes_left_out(batch, refused),
        ]
        refused_terms, term_warnings = self._unknown_terms(unnamed)
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
            run_qid=self.ensure_run_item(batch, self._identity_hash),
            warnings=warnings,
            flags=flags,
            refused_terms=refused_terms,
        )
        # the claims that added nothing to the statement they matched,
        # looked at again after the loop and the kinds (dry runs hold none)
        withheld: list[tuple] = []
        for index, claim in enumerate(batch.claims):
            if index in refused:
                # before any of its refs resolves: an item only a refused
                # claim mentions is never minted
                result.refused.append(claim_id(claim))
                result.refusal_reasons.append(reasons[index])
                if index in loops:
                    result.self_references.append(claim_id(claim))
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
            # the claim's judgement note, in its run block (ADR-0035 §4): a
            # curated claim has no run block, and so no note
            references = self._references(
                artifact_qid=artifact_qid,
                run_qid=run_qid,
                claim=claim,
                note=claim.note,
            )
            existing = self._find_matching_statement(subject_qid, wbi_claim)
            before = self._before_write(subject_qid, wbi_claim, existing)
            if existing is not None:
                blocks = self._blocks_to_add(
                    existing, references, artifact_qid, before
                )
                if not blocks:
                    result.skipped += 1
                    # a claim that adds nothing yet may add a block once the
                    # batch's other claims are written and its kinds recorded:
                    # its run's block beside a noted block of its run they gave
                    # the statement, or, skipped while the statement was a
                    # curator's own write, its blocks once a machine claim has
                    # joined it (the module docstring, step 4): looked at again
                    # then, so the batch's order changes nothing
                    withheld.append(
                        (
                            subject_qid,
                            wbi_claim,
                            references,
                            artifact_qid,
                            before,
                        )
                    )
                    continue
                self._add_blocks(subject_qid, existing, blocks, result)
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
                result.notes_written += sum(
                    map(self._noted, references.references)
                )
            result.written += 1
        # the kinds first, so an explicit `instance of` claim looked at
        # again below meets the run blocks they gave its statement
        if not self.dry_run:
            self._record_kinds(result)
        for (
            subject_qid,
            wbi_claim,
            references,
            artifact_qid,
            before,
        ) in withheld:
            existing = self._find_matching_statement(subject_qid, wbi_claim)
            blocks = (
                self._blocks_to_add(existing, references, artifact_qid, before)
                if existing is not None
                else []
            )
            if blocks:
                self._add_blocks(subject_qid, existing, blocks, result)
                result.skipped -= 1
        return result

    def _add_blocks(
        self, subject_qid: str, statement, blocks: list, result: WriteResult
    ) -> None:
        """Add a claim's `blocks` to the equal statement it matches, and
        write its item (step 4 of the module docstring): an agreeing
        source, or the same source in another place, or the run's block
        with another note, and the statement stays one statement (R2: who
        asserts a fact, and where, is knowledge, not just the fact)."""
        for block in blocks:
            statement.references.add(block)
        self._entity_cache[subject_qid].write(
            summary="ingest: additional source for existing statement"
        )
        self._entity_cache[subject_qid] = self.client.wbi.item.get(subject_qid)
        result.referenced += 1
        result.notes_written += sum(map(self._noted, blocks))
