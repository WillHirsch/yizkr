"""Adjudication of ambiguous cluster pairs: the second half of the scratch
driver's clustering stage (ADR-0007 phase 3 at corpus level; ADR-0025).

Clustering (yizkr.pipeline.clustering) links each candidate pair on
deterministic evidence alone, confidently or ambiguously, and merges
confident clusters and the confident components of ambiguous ones. This
stage asks the model about the pairs it could not decide: the ambiguous
pairs of each ambiguous candidate cluster that no merged confident
component holds, so those between components (and inside a component
its re-check kept apart), and no other. Each is asked on its own two
mentions' evidence, whatever components they lie in. A conflicting
cluster joins a cannot-link pair, and never merges whatever a verdict
says, so none of its pairs is asked; nor is a confident link, nor a
cluster ambiguous only because two members never paired contradict each
other.

**The calls.** The pairs go to the model in batches of the thresholds'
`pairs-per-call` (clustering.yaml): a cluster's pairs stay together in
one call, a cluster with more is split into calls' worth first, and calls
are filled in the review's order (pack, as clustering.adjudication_calls
counts them). Each call goes through the run's LLM cache, in the role
ROLE, whose engine, model and effort it reuses, and is keyed by what it
sends (ADR-0032): the prompt, which holds each pair's two mention
fingerprints and the evidence below, and the role's engine, model and
effort. Its calls are reported to the run's events under the role NAME
and the stage `clustering`, so the monitor and the report count them
apart from the critique's. The prompt names each pair by its number and
each mention by its fingerprint, never by a QID, and each document by the
SHA-1 of its media; a value that is an item already in the graph is named
as one, never by its QID.

**The evidence** of each mention is what clustering reads of its
manifest: its kind, label and aliases, its mention record (its name
parts, ADR-0035 §2), and every claim of its document about it, or that
names it as a value (its kin, the events it takes part in with its role,
its places), each value named by its label and kind; with clustering's
own evidence for the pair (how names, dates and kin compare, and what
keeps the pair apart). A date is rendered at its own precision, as the
site renders it (staticsite.format_time): "the 1830s", "the 19th
century", never a first year; with the bounds and circa the source
gives (rule 5).

**Verdicts.** The answer gives each pair `same`, `different` or
`uncertain`, with a one-sentence reason. An answer line outside that
schema (no pair asked, another verdict, a reason that is no text) is
refused and counted; a line that is no JSON object is skipped
uncounted. A pair answered twice differently is refused whole and
counted, and so is a pair the answer leaves out: none of them has a
verdict. An answer cut off, at the output limit or before its terminal
sentinel, is refused whole and counted, its pairs left without a
verdict: one the LLM cache kept from an earlier invocation is rejected
and asked once more; one the engine gave in this run is rejected, and
one the engine gave up on at its output limit is never kept, so the
next invocation asks again.

**Merging.** In a run of the development stage (ADR-0025), the verdicts
settle what they can, unattended: clustering is made again under them
(clustering.cluster_corpus with `verdicts`, whose docstring has the
rule). An ambiguous candidate cluster whose every pending pair is
`same` or `different` is cut at its `different` pairs, and the closure
over its confident links and its `same` pairs gives its parts: its
confident components, joined by `same` pairs. A `same` on any pair
between two components joins them whole, and a `different` on any pair
between them keeps them apart. A part merges, `adjudicated` when a
`same` pair is among its links (every member of the components it joins
included) and `confident_component` otherwise, unless it holds a pair
contradicted: a `different` pair the closure joined another way (two
components given both verdicts among them, say), a `same` pair whose
evidence says they differ by more than their own dates (a verdict
excuses a date conflict on its own pair, never stated sexes, civil
statuses or kin that conflict, a place's containers or an
organisation's places that differ, event kinds that cannot be one, or
events whose principals ended in clusters held apart), or two other
members whose evidence says they differ; then it falls
back to its confident components, each merged or apart as without
verdicts. A candidate cluster of two whose one pair no
`same` could merge (clustering.cannot_merge) is not asked, since no
verdict on it changes a cluster, and is counted
(`unmergeable_pairs_not_asked`). A cluster with an `uncertain` pending
pair, or one without a verdict, stays as it is without adjudication: its
confident components merged, the rest apart. Unions are settled after
people, and events after unions, since their links read the clusters
their people end in:
the stage asks the pending pairs of the first group of kinds
(clustering.stage_group), clusters again, and asks the next group's,
so a union whose couple adjudication merged may link confidently and
need no call. A pair asked once in an invocation is not asked again in
it. In keeper mode (a run whose manifest records another project stage)
every pending pair is asked once, each group of kinds packed apart, the
verdicts are recorded for the cluster review sheet, and no verdict
merges anything: the identity map is clustering's own, its confident
components merged.

**Outputs.** `identity-map.json` is the map the write reads; RECORD
beside it keeps every verdict with its reason, each call's pairs and raw
answer, and the counts. Both hold corpus words, so they stay in the run
directory, which is local-only data. The stage reuses both while the
clustering's inputs (clustering.inputs_key), VERSION, the prompt, this
module's source and the date formatter it renders with (code_digest),
the role's engine, model, effort and epoch, whether the run refreshes
the role, the pairs per call and whether verdicts merge all stand, and
while no call was refused; it makes them again otherwise, and the calls
it makes again are answered from the LLM cache while what they send
stands. The stage reads no wiki and no query service. What reaches the
run's events is counts within labels of its own.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yizkr.claims import (
    WIKIBASE_PRECISION,
    ClaimBatch,
    CoordinateValue,
    EntityRecord,
    ItemValue,
    LocalRef,
    MonolingualValue,
    QidRef,
    QuantityValue,
    TimeValue,
    VocabRef,
)
from yizkr.pipeline import clustering
from yizkr.pipeline.clustering import (
    AMBIGUOUS,
    CONFIDENT_COMPONENT,
    DIFFERENT,
    EVENT_KINDS,
    GEDCOM,
    IDENTITY_MAP,
    SAME,
    UNCERTAIN,
    UNION_KINDS,
    VERDICTS,
    ClusteringConfig,
    Manifest,
    Pair,
    load_config,
    pending_pairs,
    read_identity_map,
    read_manifests,
)
from yizkr.pipeline.extract import scan_json_objects
from yizkr.pipeline.llm import (
    CUT_OFF_AT_LIMIT,
    OutputLimitReached,
    at_output_limit,
    reject_response,
)
from yizkr.pipeline.rundir import DEVELOPMENT_STAGE
from yizkr.pipeline.stages import (
    atomic_write_text,
    canonical_json,
    sha256_text,
)

# what the stage makes of the same verdicts and inputs: bumped whenever it
# would make another map or record of them
VERSION = 1
# the layout of RECORD
FORMAT = 1
# the corpus stage it runs in, and the name its calls are reported under
STAGE = clustering.STAGE
NAME = "adjudication"
# the role whose engine, model and effort it calls: the run's main model
# at the review effort, which ADR-0016 gives the judgement steps
ROLE = "critique"
# its record of verdicts, calls and counts, beside the identity map
RECORD = "adjudication.json"
# how many claims of a mention the prompt gives, each way, at most
MAX_CLAIMS = 30
_QID = re.compile(r"\bQ[1-9]\d*\b")

PROMPT = """\
You adjudicate IDENTITY for a family archive. Each numbered pair below
names two mentions, A and B, each from a source document, with what
that source says about it. Deterministic clustering found the two alike
but could not decide whether they are one entity: decide, from the
evidence the sources give, whether they are the SAME entity, DIFFERENT
entities, or whether the evidence cannot tell.

Weigh relational evidence first: kin and spouses, dates, places, roles
in events. A shared name alone is weak evidence, a common surname
weaker still: two people of one name are often two people. Sources
disagree about dates and spellings and can still describe one person,
so a difference is evidence, not proof. A date is given at the
precision its source states: "the 1830s" is a decade and "the 19th
century" a century, never a year within them. Answer `different` only
when the evidence shows two entities, and `uncertain` when it does not
decide; never guess.

Each pair also gives clustering's comparison as name/dates/kin (how the
names, the dates and the kin compare) and, after a further slash, what
kept it from linking, when something did.

For every pair output one NDJSON line:
{{"pair": <its number>, "verdict": "same" | "different" | "uncertain",
"reason": "<one sentence>"}}
No wrapper object, no code fences, no prose. End with exactly:
{{"done": true}}

Pairs:
{pairs_block}
"""


# -- the evidence -------------------------------------------------------------


def date_text(value: TimeValue) -> str:
    """A date at the precision its source states, as the site renders it
    (staticsite.format_time): a decade as "the 1830s", a century as "the
    19th century", a year, a month or a day; never a first year."""
    from yizkr.staticsite import format_time

    precision = value.precision or (
        "day" if value.day else "month" if value.month else "year"
    )
    stamp = (
        f"+{value.year:04d}-{value.month or 1:02d}-{value.day or 1:02d}"
        "T00:00:00Z"
    )
    text = format_time(stamp, str(WIKIBASE_PRECISION[precision]))
    return f"the {text}" if precision in ("century", "decade") else text


def record_line(entity: EntityRecord) -> str:
    """An entity's mention record as the prompt shows it: each field it
    fills, a place's containers by name, nearest first (their evidence
    is the extraction's to check, ADR-0035 §5)."""
    record = {
        name: part
        for name, part in entity.structure.model_dump(
            mode="json", exclude_none=True
        ).items()
        if part not in ([], {}, "") and name != "kind"
    }
    if "within" in record:
        record["within"] = [
            container["name"] for container in record["within"]
        ]
    return "record: " + json.dumps(
        record, ensure_ascii=False, separators=(", ", ": ")
    )


class _Document:
    """What the prompt reads of one document's batch: each local id's
    label and kind, and the claims about each, as subject and as value.
    An entity a mention's claims name is given one level deep: an event
    or a union with its own claims (its date, its place, its other
    participants with their roles), anything else with its own dates."""

    def __init__(self, manifest: Manifest) -> None:
        batch: ClaimBatch = manifest.batch
        self.sha1 = manifest.sha1.lower()
        self.gedcom = manifest.source == GEDCOM
        self.entities = {entity.id: entity for entity in batch.entities}
        self.names: dict[str, tuple[str, str]] = {
            entity.id: (entity.label, entity.kind or "")
            for entity in batch.entities
        }
        self.about: dict[str, list[Any]] = {}
        self.naming: dict[str, list[Any]] = {}
        for claim in batch.claims:
            subject = claim.subject
            if isinstance(subject, LocalRef):
                self.names.setdefault(
                    subject.local_id,
                    (subject.label or "", subject.instance_of or ""),
                )
                self.about.setdefault(subject.local_id, []).append(claim)
            value = claim.value
            if isinstance(value, ItemValue) and isinstance(
                value.value, LocalRef
            ):
                ref = value.value
                self.names.setdefault(
                    ref.local_id, (ref.label or "", ref.instance_of or "")
                )
                if not isinstance(subject, LocalRef) or (
                    subject.local_id != ref.local_id
                ):
                    self.naming.setdefault(ref.local_id, []).append(claim)

    def named(self, ref: Any) -> str:
        """An entity a claim names: a local id by its label and kind, a
        vocabulary item by its key, an item already in the graph as one,
        never by its QID."""
        if isinstance(ref, LocalRef):
            label, kind = self.names.get(ref.local_id, (ref.label or "", ""))
            return f"{label!r} ({kind})" if kind else repr(label)
        if isinstance(ref, VocabRef):
            return ref.key
        if isinstance(ref, QidRef):
            return "an item already in the graph"
        return "an entity"

    def value(self, value: Any) -> str:
        if isinstance(value, TimeValue):
            return date_text(value)
        if isinstance(value, ItemValue):
            return self.named(value.value)
        if isinstance(value, MonolingualValue):
            return repr(value.value.text)
        if isinstance(value, QuantityValue):
            return str(value.amount)
        if isinstance(value, CoordinateValue):
            return f"{value.latitude}, {value.longitude}"
        return repr(str(getattr(value, "value", "")))

    def qualifiers(self, claim: Any) -> str:
        parts = []
        for qualifier in claim.qualifiers:
            shown = self.value(qualifier.value)
            if qualifier.property == "object_has_role":
                parts.append(f"role: {shown}")
            elif qualifier.property == "sourcing_circumstances":
                parts.append(shown)
            else:
                parts.append(f"{qualifier.property}: {shown}")
        return f" [{'; '.join(parts)}]" if parts else ""

    def lines(self, local_id: str) -> list[str]:
        """What the document says of the entity: its label, aliases and
        record, its claims, and the claims that name it."""
        label, kind = self.names.get(local_id, ("", ""))
        entity = self.entities.get(local_id)
        found = [f"label: {label!r}"]
        if entity is not None and entity.aliases:
            found.append(
                "aliases: "
                + ", ".join(repr(alias) for alias in entity.aliases)
            )
        if entity is not None and entity.structure is not None:
            found.append(record_line(entity))
        about = self.about.get(local_id, [])
        found += [
            f"{claim.property}: {self.value(claim.value)}"
            + self.qualifiers(claim)
            + (
                self.brief(self._local(claim.value.value), local_id)
                if isinstance(claim.value, ItemValue)
                else ""
            )
            for claim in about[:MAX_CLAIMS]
        ]
        if len(about) > MAX_CLAIMS:
            found.append(f"and {len(about) - MAX_CLAIMS} claims more")
        naming = self.naming.get(local_id, [])
        found += [
            f"named as the {claim.property} of {self.named(claim.subject)}"
            + self.qualifiers(claim)
            + self.brief(self._local(claim.subject), local_id)
            for claim in naming[:MAX_CLAIMS]
        ]
        if len(naming) > MAX_CLAIMS:
            found.append(
                f"and named in {len(naming) - MAX_CLAIMS} claims more"
            )
        return found

    def _local(self, ref: Any) -> str | None:
        return ref.local_id if isinstance(ref, LocalRef) else None

    def brief(self, local_id: str | None, of: str) -> str:
        """What the document says of an entity a claim of `of` names, one
        level deep, after a dash; empty when it says nothing more. An
        event or a union gives its claims but those that name `of`; any
        other entity its own dates."""
        if local_id is None or local_id == of:
            return ""
        kind = self.names.get(local_id, ("", ""))[1]
        timed = kind in EVENT_KINDS or kind in UNION_KINDS
        claims = [
            claim
            for claim in self.about.get(local_id, [])
            if (
                timed
                and not (
                    isinstance(claim.value, ItemValue)
                    and self._local(claim.value.value) == of
                )
            )
            or (not timed and isinstance(claim.value, TimeValue))
        ]
        if not claims:
            return ""
        shown = [
            f"{claim.property}: {self.value(claim.value)}"
            + self.qualifiers(claim)
            for claim in claims[:MAX_CLAIMS]
        ]
        if len(claims) > MAX_CLAIMS:
            shown.append(f"and {len(claims) - MAX_CLAIMS} claims more")
        return " — " + "; ".join(shown)


class Evidence:
    """Each mention's evidence, as the prompt gives it, from the manifests
    and the identity map's record of each mention (its document and
    local id)."""

    def __init__(
        self, manifests: Sequence[Manifest], mentions: Mapping[str, Any]
    ) -> None:
        self._documents: dict[str, _Document] = {}
        for manifest in manifests:
            self._documents.setdefault(
                manifest.sha1.lower(), _Document(manifest)
            )
        self._mentions = mentions

    def mention(self, fingerprint: str, letter: str) -> list[str]:
        entry = self._mentions[fingerprint]
        document = self._documents[str(entry["document"]).lower()]
        source = "a GEDCOM export" if document.gedcom else "a source document"
        head = (
            f"Mention {letter}: fingerprint {fingerprint}, a "
            f"{entry.get('kind') or 'mention of no kind'} in {source}, "
            f"document {document.sha1}"
        )
        return [head] + [
            f"  {line}" for line in document.lines(entry["local_id"])
        ]


def pair_text(pair: Pair, link: str, evidence: Evidence) -> str:
    """A pair as the prompt gives it, without its number."""
    lines = [f"clustering's comparison: {link}"]
    for letter, fingerprint in zip("AB", pair, strict=True):
        lines += [
            f"  {line}" for line in evidence.mention(fingerprint, letter)
        ]
    return "\n".join(lines)


def prompt_for(
    pairs: Sequence[Pair], links: Mapping[Pair, str], evidence: Evidence
) -> str:
    """The prompt that asks one batch of pairs, numbered from 1. No QID
    is ever written into it."""
    block = "\n\n".join(
        f"{number}. {pair_text(pair, links.get(pair, ''), evidence)}"
        for number, pair in enumerate(pairs, start=1)
    )
    return PROMPT.format(pairs_block=_QID.sub("an item", block))


# -- batches and answers ------------------------------------------------------


def pack(groups: Iterable[Sequence[Pair]], per_call: int) -> list[list[Pair]]:
    """The calls a list of clusters' pairs takes, `per_call` pairs a call:
    a cluster's pairs stay together, a cluster larger than a call is split
    into calls' worth first, and calls are filled in order
    (clustering.adjudication_calls counts them)."""
    calls: list[list[Pair]] = []
    current: list[Pair] = []
    for pairs in groups:
        for start in range(0, len(pairs), per_call):
            chunk = list(pairs[start : start + per_call])
            if current and len(current) + len(chunk) > per_call:
                calls.append(current)
                current = []
            current += chunk
    if current:
        calls.append(current)
    return calls


@dataclass
class Answers:
    """What one answer says: each pair's (verdict, reason) by its number;
    the lines outside the schema (`rejected`); and the pairs answered
    twice or more, differently (`conflicts`), each refused whole."""

    verdicts: dict[int, tuple[str, str]] = field(default_factory=dict)
    rejected: int = 0
    conflicts: list[int] = field(default_factory=list)


def parse_answers(
    response: str, count: int, *, cut_off: bool = False
) -> Answers:
    """The verdicts of an answer to `count` pairs, numbered from 1. Raises
    ValueError for an answer cut off: one the engine stopped at its output
    limit (`cut_off`), or one without its terminal sentinel, a line whose
    `done` is true."""
    found = Answers()
    finished = False
    given: dict[int, set[tuple[str, str]]] = {}
    for obj in scan_json_objects(response):
        if obj.get("done") is True:
            finished = True
            continue
        number = obj.get("pair")
        verdict = obj.get("verdict")
        reason = obj.get("reason", "")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or not 1 <= number <= count
            or verdict not in VERDICTS
            or not isinstance(reason, str)
        ):
            found.rejected += 1
            continue
        given.setdefault(number, set()).add((verdict, reason))
    if cut_off:
        raise ValueError(f"adjudication answer {CUT_OFF_AT_LIMIT}")
    if not finished:
        raise ValueError("adjudication answer carried no terminal sentinel")
    for number in sorted(given):
        answers = given[number]
        if len({verdict for verdict, _reason in answers}) > 1:
            found.conflicts.append(number)
            continue
        found.verdicts[number] = sorted(answers)[0]
    return found


# -- the stage ----------------------------------------------------------------


@dataclass
class Adjudicated:
    """What the stage made: the identity map, every verdict with its
    reason (pair -> (verdict, reason)), each call's record, and the
    counts."""

    identity: dict[str, Any]
    verdicts: dict[Pair, tuple[str, str]]
    calls: list[dict[str, Any]]
    counts: dict[str, Any]


def _links(identity: Mapping[str, Any]) -> dict[Pair, str]:
    return {
        clustering._pair(*pair["mentions"]): pair["evidence"]
        for entry in identity["review"]
        for pair in entry.get("pairs") or ()
        if pair.get("link") == AMBIGUOUS
    }


def adjudicate(
    manifests: Sequence[Manifest],
    client: Callable[[], Any],
    *,
    merge: bool,
    config: ClusteringConfig | None = None,
    log: Callable[..., None] = lambda *args, **kwargs: None,
) -> Adjudicated:
    """Cluster the manifests, ask the pending pairs (the module docstring
    says which, in what order, and what merges), and cluster again under
    the verdicts when they `merge`. `client` gives the client the calls go
    through, and is called only when a pair is to be asked."""
    config = config or load_config()
    per_call = config.adjudication.pairs_per_call
    identity = clustering.cluster_corpus(manifests, config)
    planned = identity["summary"]["adjudication"]["calls_ambiguous_clusters"]
    evidence = Evidence(manifests, identity["mentions"])
    verdicts: dict[Pair, tuple[str, str]] = {}
    asked: set[Pair] = set()
    calls: list[dict[str, Any]] = []
    tally: Counter[str] = Counter()
    engine: Any = None
    rounds = 0
    while True:
        pending = [
            (group, [pair for pair in pairs if pair not in asked])
            for group, pairs in pending_pairs(
                identity["review"], merging=merge
            )
        ]
        pending = [(group, pairs) for group, pairs in pending if pairs]
        if not pending:
            break
        if merge:
            first = min(group for group, _pairs in pending)
            pending = [(g, pairs) for g, pairs in pending if g == first]
        rounds += 1
        links = _links(identity)
        # each group of kinds packed apart, as calls_ambiguous_clusters
        # plans them
        batches = [
            batch
            for group in sorted({group for group, _pairs in pending})
            for batch in pack(
                (pairs for g, pairs in pending if g == group), per_call
            )
        ]
        if engine is None:
            engine = client()
        log(
            f"{NAME}: round {rounds}: asking {sum(map(len, batches))} "
            f"ambiguous pairs in {len(batches)} calls of up to {per_call}"
        )
        for batch in batches:
            asked.update(batch)
            calls.append(
                _ask(engine, batch, links, evidence, verdicts, tally, rounds)
            )
        if not merge:
            break
        identity = clustering.cluster_corpus(
            manifests,
            config,
            {pair: verdict for pair, (verdict, _reason) in verdicts.items()},
        )
    # the clusters no verdict could merge, left unasked when verdicts
    # merge, counted on the last map: a cluster of events can become one
    # only once its people's verdicts are in, and none is ever asked
    unmergeable = (
        sum(
            clustering.cannot_merge(entry)
            for entry in identity["review"]
            if entry.get("status") == AMBIGUOUS
        )
        if merge
        else 0
    )
    found = Counter(verdict for verdict, _reason in verdicts.values())
    counts = {
        "merge": merge,
        "role": ROLE,
        "planned_calls": planned,
        "rounds": rounds,
        "calls": len(calls),
        "asked_pairs": len(asked),
        "unmergeable_pairs_not_asked": unmergeable,
        "verdicts": {verdict: found[verdict] for verdict in VERDICTS},
        "unanswered_pairs": len(asked) - len(verdicts),
        "rejected_lines": tally["rejected"],
        "conflicting_answers": tally["conflicts"],
        "refused_calls": tally["refused"],
        "second_attempts": tally["again"],
    }
    identity["summary"]["adjudication"].update(counts)
    return Adjudicated(identity, verdicts, calls, counts)


def _ask(
    engine: Any,
    batch: list[Pair],
    links: Mapping[Pair, str],
    evidence: Evidence,
    verdicts: dict[Pair, tuple[str, str]],
    tally: Counter[str],
    round_number: int,
) -> dict[str, Any]:
    """Ask one batch, keep its verdicts, and return the call's record."""
    prompt = prompt_for(batch, links, evidence)
    response = ""
    refused = None
    answers = None

    def parsed(response: str) -> Answers:
        return parse_answers(
            response, len(batch), cut_off=at_output_limit(engine, response)
        )

    try:
        response = engine.complete(prompt)
        try:
            answers = parsed(response)
        except ValueError as exc:
            # an answer the LLM cache kept from an earlier invocation is
            # rejected and asked once more; one the engine gave in this
            # run is rejected, so the next invocation asks again, and
            # refused
            if reject_response(engine, response, f"{NAME}: {exc}"):
                tally["again"] += 1
                # the call's record never keeps the answer it rejected
                response = ""
                response = engine.complete(prompt)
                try:
                    answers = parsed(response)
                except ValueError as again:
                    reject_response(engine, response, f"{NAME}: {again}")
                    refused = str(again)
            else:
                refused = str(exc)
    except OutputLimitReached:
        # the engine gave up at its output limit: nothing is kept, so the
        # next invocation asks again
        refused = f"adjudication answer {CUT_OFF_AT_LIMIT}"
    if answers is None:
        tally["refused"] += 1
        answers = Answers()
    tally["rejected"] += answers.rejected
    tally["conflicts"] += len(answers.conflicts)
    for number, answer in answers.verdicts.items():
        verdicts[batch[number - 1]] = answer
    print(
        f"  {NAME}: {len(batch)} pairs -> {len(answers.verdicts)} verdicts"
        + (f" (refused: {refused})" if refused else ""),
        file=sys.stderr,
    )
    return {
        "round": round_number,
        "pairs": [list(pair) for pair in batch],
        "response": response,
        "refused": refused,
        "rejected_lines": answers.rejected,
        "conflicts": answers.conflicts,
    }


def model_of(driver: Any) -> dict[str, Any]:
    """The engine, model and effort the stage calls (ROLE), as the driver
    configures the role, with the role's epoch and whether the run
    refreshes it: what its record's key names. So `--refresh critique`
    makes the stage's outputs again once, asking the engine afresh, and
    an invocation that refreshes it again reuses them."""
    effort, _ = driver._engine_settings(ROLE)
    model, _ = driver._configured_model(ROLE)
    return {
        "engine": driver.llm,
        "model": model,
        "effort": effort,
        "epoch": (getattr(driver, "epochs", None) or {}).get(ROLE, 0),
        "refresh": ROLE in (getattr(driver, "refresh", None) or ()),
    }


def stage_key(
    manifests: Sequence[Manifest],
    config: ClusteringConfig,
    model: Mapping[str, Any],
    merge: bool,
) -> str:
    """The key the stage's outputs are reused under (the module
    docstring). The pairs per call are among the thresholds, which
    clustering's inputs key covers."""
    return sha256_text(
        canonical_json(
            {
                "stage": NAME,
                "version": VERSION,
                "clustering": clustering.inputs_key(manifests, config),
                "prompt": sha256_text(PROMPT),
                "code": code_digest(),
                "model": dict(model),
                "merge": merge,
            }
        )
    )


def code_digest() -> str:
    """The SHA-256 of what renders and reads the calls: this module's
    source, and the site's date formatter it renders dates with."""
    from yizkr import staticsite

    return sha256_text(
        canonical_json(
            {
                "module": hashlib.sha256(
                    Path(__file__).read_bytes()
                ).hexdigest(),
                "dates": sha256_text(
                    inspect.getsource(staticsite.format_time)
                    + inspect.getsource(staticsite.ordinal)
                ),
            }
        )
    )


def read_record(directory: Path) -> dict[str, Any] | None:
    try:
        record = json.loads((directory / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(record, dict) or record.get("format") != FORMAT:
        return None
    return record


def merges_on_verdicts(run: Any) -> bool:
    """Whether a run's verdicts merge unattended: a run of the development
    stage (ADR-0025), as its manifest records it."""
    return run.manifest().get("project_stage") == DEVELOPMENT_STAGE


def client_of(context: Any) -> Any:
    """The client the stage calls the engine through: the run's engine in
    ROLE, behind the run's LLM cache and keyed as that role's calls are,
    every call and what the cache does reported to the run's events under
    the role NAME and the stage `clustering`, so the report and the
    monitor count them apart from the critique's."""

    def emit(event: str, **fields: Any) -> Any:
        if "role" in fields:
            fields["role"] = NAME
        if fields.get("stage") is not None:
            fields["stage"] = STAGE
        return context.run.emit(event, **fields)

    return context.driver.role_client(ROLE, events=emit)


@dataclass(frozen=True)
class StageOutput:
    """The identity map the stage holds, its record, and whether both were
    reused rather than made."""

    identity_map: dict[str, Any]
    record: dict[str, Any]
    reused: bool


def ensure_adjudicated(
    context: Any, config: ClusteringConfig | None = None
) -> StageOutput:
    """The identity map and the record of the context's documents, in its
    directory: those there when made under the same key (stage_key), or
    made now and written there whole, the map first."""
    config = config or load_config()
    manifests = read_manifests(context.documents)
    merge = merges_on_verdicts(context.run)
    key = stage_key(manifests, config, model_of(context.driver), merge)
    directory: Path = context.directory
    path = directory / IDENTITY_MAP
    record = read_record(directory)
    stored = read_identity_map(path)
    if (
        record is not None
        and stored is not None
        and record.get("key") == key
        and record.get("map") == stored.get("inputs")
        and not (record.get("counts") or {}).get("refused_calls")
    ):
        return StageOutput(stored, record, True)
    made = adjudicate(
        manifests,
        lambda: client_of(context),
        merge=merge,
        config=config,
        log=context.log,
    )
    record = {
        "format": FORMAT,
        "version": VERSION,
        "key": key,
        "map": made.identity["inputs"],
        "counts": made.counts,
        "verdicts": [
            {"mentions": list(pair), "verdict": verdict, "reason": reason}
            for pair, (verdict, reason) in sorted(made.verdicts.items())
        ],
        "calls": made.calls,
    }
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path, json.dumps(made.identity, ensure_ascii=False, indent=1) + "\n"
    )
    atomic_write_text(
        directory / RECORD,
        json.dumps(record, ensure_ascii=False, indent=1) + "\n",
    )
    return StageOutput(made.identity, record, False)


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def attention(summary: Mapping[str, Any]) -> list[str]:
    """The reasons the adjudicated map needs attention: clustering's,
    with what adjudication left undecided or refused in place of its
    estimate."""
    counts = summary["adjudication"]
    reasons = clustering.attention(summary, adjudicated=True)
    if counts["unanswered_pairs"]:
        reasons.append(
            "adjudicated pairs without a verdict: "
            f"{_counted(counts['unanswered_pairs'], 'pair')}"
        )
    if counts["rejected_lines"] or counts["refused_calls"]:
        reasons.append(
            "adjudication answers refused: "
            f"{_counted(counts['rejected_lines'], 'line')} outside the "
            f"schema, {_counted(counts['refused_calls'], 'call')} cut off"
        )
    if counts["conflicting_answers"]:
        reasons.append(
            "adjudication answers in conflict: "
            f"{_counted(counts['conflicting_answers'], 'pair')}"
        )
    return reasons


def run_stage(context: Any) -> list[str]:
    """The corpus stage `clustering`: the identity map of the context's
    documents, adjudicated (ensure_adjudicated), in its directory. Without
    a driver to call the engine through, the map is clustering's own
    (clustering.run_stage). Returns the reasons it needs attention."""
    if context.driver is None:
        return clustering.run_stage(context)
    output = ensure_adjudicated(context)
    summary = output.identity_map["summary"]
    counts = summary["adjudication"]
    found = counts["verdicts"]
    context.log(
        f"{STAGE}: identity map "
        + ("reused, its inputs unchanged" if output.reused else "made")
        + f": {_counted(summary['clustered_mentions'], 'mention')} of "
        f"{_counted(summary['documents'], 'document')} in "
        f"{_counted(summary['identity_clusters'], 'cluster')}, "
        f"{_counted(summary['merged_mentions'], 'mention')} merged, "
        f"{summary['cluster_mentions'][CONFIDENT_COMPONENT]} of them by the "
        "confident components of ambiguous clusters; "
        f"{NAME}: {_counted(counts['asked_pairs'], 'pair')} in "
        f"{_counted(counts['calls'], 'call')}, {found[SAME]} same, "
        f"{found[DIFFERENT]} different, {found[UNCERTAIN]} uncertain; "
        f"{summary['clusters']['adjudicated']} clusters merged on verdicts"
        + ("" if counts["merge"] else " (none: verdicts recorded only)")
        + f"; {summary['clusters'][AMBIGUOUS]} ambiguous and "
        f"{summary['clusters']['conflicting']} conflicting clusters not "
        "merged"
    )
    return attention(summary)
