"""Generated fictional cohorts taken through the reviewed assignment path.

`generate` builds a cohort of a given `Scale`: its sources, the people
each names, the share of a source's mentions that name a person an earlier
source introduced, and the dated claims each makes, drawn from the example
universe's cohort tables (docs/EXAMPLES.md, "Generated cohorts").
`saved_products` saves every source's document views with the production
scheduler, from authored answers that state exactly what the source says.
`run_cohort` takes the cohort through `SourceIngestion`, one source after
another: registration, identity review and application, and claim review
and application, with explicit human choices (an `assign` to the person's
entity for a mention an earlier source introduced, `new_entity` otherwise)
and approvals. It records each operation's requests by kind, the records
written, the review-store calls and the graph's size before it, through a
`RequestLog`: against `fake_wikibase.FakeWikibase` offline, or a real stack
through `fake_wikibase.observing`.

Identity application creates at most one entity per protected operation,
so each mention is applied by its own call; a source's claims are applied
together. The caller's `synchronize` runs before each identity preparation
and application, when the query service must have indexed every earlier
write, and its requests and time are recorded apart.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from tests.fixtures import example_universe as universe
from tests.fixtures.document_processing import IsolatedDriver, quiet
from tests.fixtures.fake_wikibase import RequestLog

from yizkr.core.write_recovery import CreationRegistry
from yizkr.pipeline.claim_review import record_claim_decisions
from yizkr.pipeline.identity_review import ClaimDecisionInput, ReviewStore
from yizkr.pipeline.review_stages import MentionKey, ReviewProposal
from yizkr.pipeline.source_ingestion import SourceIngestion

ACTOR = "fictional cohort curator"
WHEN = "2026-01-01T12:00:00Z"
ARTIFACT_TYPE = "certificate"
# the claims a person's register line makes, in the order a source's claims
# take them
PROPERTIES = ("date_of_birth", "date_of_death")


@dataclass(frozen=True)
class Scale:
    """A cohort's size: `sources` registers of `mentions` people each,
    of which a `shared` share in every register after the first names a
    person an earlier register introduced, and `claims` dated claims per
    register, at most two per person (birth and death). `existing` people
    are in the graph before the first register, each an item of its own
    that no register names."""

    sources: int
    mentions: int
    shared: float
    claims: int
    existing: int = 0
    seed: int = universe.COHORT_SEED

    def __post_init__(self):
        if self.sources < 1 or self.mentions < 1 or self.existing < 0:
            raise ValueError("a cohort needs a source and a mention")
        if not 0 <= self.shared <= 1:
            raise ValueError("the shared share is between 0 and 1")
        if not 0 <= self.claims <= len(PROPERTIES) * self.mentions:
            raise ValueError("a register makes at most two claims a person")

    @property
    def shared_mentions(self) -> int:
        """How many mentions of a register after the first name a person
        an earlier register introduced."""
        return round(self.shared * self.mentions)


# The benchmark's sizes (scripts/assignment_cost.py): the smallest is the
# regression scenario whose request counts the offline check keeps and the
# scale of the fictional-lane measurement; the others grow the cohort fifty
# times in identities and the graph it meets fifty times in entities.
SIZES = {
    "small": Scale(sources=2, mentions=1, shared=1.0, claims=1),
    "medium": Scale(
        sources=10, mentions=2, shared=0.5, claims=2, existing=1000
    ),
    "large": Scale(
        sources=50, mentions=2, shared=0.5, claims=2, existing=10000
    ),
}
REGRESSION = SIZES["small"]


@dataclass(frozen=True)
class Person:
    key: str
    given: str
    surname: str
    born: int
    died: int

    @property
    def name(self) -> str:
        return f"{self.given} {self.surname}"


@dataclass(frozen=True)
class Mention:
    local_id: str
    person: Person
    line: int
    shared: bool

    @property
    def text(self) -> str:
        return (
            f"{self.line - 1}. {self.person.name}, geb. {self.person.born}, "
            f"gest. {self.person.died}."
        )


@dataclass(frozen=True)
class CohortClaim:
    mention: Mention
    property: str

    @property
    def year(self) -> int:
        person = self.mention.person
        return person.born if self.property == "date_of_birth" else person.died

    @property
    def excerpt(self) -> str:
        person = self.mention.person
        birth = f"{person.name}, geb. {person.born}"
        if self.property == "date_of_birth":
            return birth
        return f"{birth}, gest. {person.died}"


@dataclass(frozen=True)
class CohortSource:
    number: int
    town: str
    mentions: tuple[Mention, ...]
    claims: tuple[CohortClaim, ...]

    @property
    def name(self) -> str:
        return f"familienregister-blatt-{self.number:04d}.txt"

    @property
    def title(self) -> str:
        return f"Familienregister {self.town}, Blatt {self.number}"

    @property
    def text(self) -> str:
        return "\n".join(
            [self.title, *(mention.text for mention in self.mentions)]
        )

    def answers(self, sha1: str) -> dict[str, list[str]]:
        """The authored extraction and critique answers for this register:
        each person a human mention on its line, each claim its year,
        quoting the line's passage that states it."""
        manifest = {
            "entities": [
                {
                    "id": mention.local_id,
                    "label": mention.person.name,
                    "kind": "human",
                    "mention_lines": [mention.line],
                }
                for mention in self.mentions
            ],
            "preparation": None,
        }
        claims = [
            {
                "subject": {
                    "kind": "local",
                    "local_id": claim.mention.local_id,
                    "label": claim.mention.person.name,
                },
                "property": claim.property,
                "value": {"type": "time", "year": claim.year},
                "source": {
                    "artifact": {"kind": "artifact", "sha1": sha1},
                    "transcription_excerpt": {
                        "text": claim.excerpt,
                        "language": "de",
                    },
                },
            }
            for claim in self.claims
        ]
        lines = [json.dumps(claim, ensure_ascii=False) for claim in claims]
        return {
            "extract": [
                json.dumps(manifest, ensure_ascii=False),
                "\n".join([*lines, '{"done":true}']),
            ],
            "critique": ['{"done":true}'],
        }


@dataclass(frozen=True)
class Cohort:
    scale: Scale
    people: tuple[Person, ...]
    sources: tuple[CohortSource, ...]
    existing: tuple[Person, ...] = ()

    @property
    def identities(self) -> int:
        return sum(len(source.mentions) for source in self.sources)

    @property
    def claims(self) -> int:
        return sum(len(source.claims) for source in self.sources)

    def names(self) -> dict[str, int]:
        """The distribution of the cohort's names: distinct given names,
        surnames and full names, and the most people sharing one given
        name or one surname."""
        given = Counter(person.given for person in self.people)
        surnames = Counter(person.surname for person in self.people)
        return {
            "people": len(self.people),
            "given_names": len(given),
            "surnames": len(surnames),
            "full_names": len({person.name for person in self.people}),
            "most_sharing_a_given_name": max(given.values()),
            "most_sharing_a_surname": max(surnames.values()),
        }

    def describe(self) -> dict:
        return {
            "sources": len(self.sources),
            "mentions_per_source": self.scale.mentions,
            "shared_share": self.scale.shared,
            "claims_per_source": self.scale.claims,
            "existing_people": len(self.existing),
            "seed": self.scale.seed,
            "entities": len(self.people),
            "identities": self.identities,
            "claims": self.claims,
            "names": self.names(),
        }


def _town(key: str) -> str:
    return next(place.label for place in universe.PLACES if place.key == key)


def generate(scale: Scale) -> Cohort:
    """The cohort of `scale`, drawn with `scale.seed`: the same scale gives
    the same cohort."""
    draw = random.Random(scale.seed)
    starts, ends = universe.COHORT_SURNAME_SYLLABLES
    first, last = universe.COHORT_BIRTH_YEARS
    names = [
        (given, start + end)
        for given in universe.COHORT_GIVEN_NAMES
        for start in starts
        for end in ends
    ]
    needed = (
        scale.mentions
        + (scale.sources - 1) * (scale.mentions - scale.shared_mentions)
        + scale.existing
    )
    if needed > len(names):
        raise ValueError(f"the tables name at most {len(names)} people")
    people: list[Person] = []
    sources: list[CohortSource] = []
    for number in range(1, scale.sources + 1):
        shared = scale.shared_mentions if number > 1 else 0
        known = list(people)
        chosen = draw.sample(known, shared) if shared else []
        for _ in range(scale.mentions - shared):
            given, surname = names.pop(draw.randrange(len(names)))
            born = draw.randint(first, last)
            people.append(
                Person(
                    key=f"cohort-person-{len(people) + 1:04d}",
                    given=given,
                    surname=surname,
                    born=born,
                    died=min(born + draw.randint(20, 89), 1945),
                )
            )
            chosen.append(people[-1])
        draw.shuffle(chosen)
        mentions = tuple(
            Mention(
                local_id=f"p{index}",
                person=person,
                line=index + 1,
                shared=person in known,
            )
            for index, person in enumerate(chosen, 1)
        )
        claims = tuple(
            CohortClaim(mention, prop)
            for prop in PROPERTIES
            for mention in mentions
        )[: scale.claims]
        sources.append(
            CohortSource(
                number=number,
                town=_town(draw.choice(universe.COHORT_TOWNS)),
                mentions=mentions,
                claims=claims,
            )
        )
    # drawn after the registers' people, so that the registers of a scale
    # are the same whatever the graph held before them
    existing = []
    for index in range(1, scale.existing + 1):
        given, surname = names.pop(draw.randrange(len(names)))
        born = draw.randint(first, last)
        existing.append(
            Person(
                key=f"cohort-existing-{index:05d}",
                given=given,
                surname=surname,
                born=born,
                died=min(born + draw.randint(20, 89), 1945),
            )
        )
    return Cohort(
        scale=scale,
        people=tuple(people),
        sources=tuple(sources),
        existing=tuple(existing),
    )


def add_existing(client, cohort: Cohort) -> None:
    """Make the cohort's existing people in the graph, each an item with
    its name as label and an `instance of` human statement, through the
    client's ordinary item writes: items without registry bindings, which
    grow the graph's entities and none of its bindings, authorizations or
    assignments. The deployment generation must exist first."""
    from wikibaseintegrator.datatypes import Item

    properties, items = client.vocabulary()
    for person in cohort.existing:
        item = client.wbi.item.new()
        item.labels.set("en", person.name)
        item.descriptions.set("en", f"Existing person {person.key}")
        item.claims.add(
            Item(prop_nr=properties["instance_of"], value=items["human"])
        )
        item.write(summary="fixture: an existing person")


# -- saved document products --------------------------------------------------


class _RoutedAnswers:
    """One role's authored answers for every register of a run, each
    register's in the order its stage asks, found by the register's title
    in the prompt."""

    model_identifier = "authored-fictional-cohort"

    def __init__(self, answers: dict[str, list[str]]) -> None:
        self.answers = {
            title: list(listed) for title, listed in answers.items()
        }
        self.titles = {
            title: re.compile(re.escape(title) + r"(?!\d)")
            for title in answers
        }
        self.lock = threading.Lock()

    def complete(self, prompt, image_paths=()):
        with self.lock:
            (title,) = [
                title
                for title, pattern in self.titles.items()
                if pattern.search(prompt)
            ]
            listed = self.answers[title]
            return listed.pop(0) if len(listed) > 1 else listed[0]

    def reject(self, response, reason):
        return False


class _CohortDriver(IsolatedDriver):
    def __init__(self, folder, state_dir, *, answers, **kwargs):
        kwargs.setdefault("log", quiet)
        super().__init__(folder, state_dir, **kwargs)
        self.answers = {
            role: _RoutedAnswers(listed) for role, listed in answers.items()
        }

    def role_client(self, role, *, events=None):
        return self.answers[role]


@dataclass(frozen=True)
class SavedSource:
    source: CohortSource
    path: Path
    document_dir: Path
    sha1: str


def saved_products(cohort: Cohort, directory: Path) -> tuple[SavedSource, ...]:
    """Every register's file and its saved document views, in `directory`,
    made by one run of the production scheduler with authored answers."""
    folder = directory / "sources"
    folder.mkdir(parents=True, exist_ok=True)
    answers: dict[str, dict[str, list[str]]] = {}
    for source in cohort.sources:
        path = folder / source.name
        path.write_text(source.text + "\n")
        sha1 = hashlib.sha1(path.read_bytes()).hexdigest()
        for role, listed in source.answers(sha1).items():
            answers.setdefault(role, {})[source.title] = listed
    driver = _CohortDriver(
        folder, directory / "run", answers=answers, language="de"
    )
    if driver.run() != 0:
        raise RuntimeError("the scheduler did not save every register")
    documents = {doc.source.name: doc for doc in driver.documents()}
    saved = []
    for source in cohort.sources:
        doc = documents[source.name]
        saved.append(
            SavedSource(
                source=source,
                path=doc.source,
                document_dir=doc.dir,
                sha1=hashlib.sha1(doc.source.read_bytes()).hexdigest(),
            )
        )
    return tuple(saved)


# -- measured runs ------------------------------------------------------------


class CountingReviewStore(ReviewStore):
    """A review store that counts its public calls, and the proposals
    `list_proposals` reads, in a RequestLog, as "store:<method>" and
    "store-rows:list_proposals"."""

    def __init__(self, path: Path, log: RequestLog) -> None:
        self.log = log
        super().__init__(path)

    def size(self) -> int:
        """How many identity proposals the store holds, read uncounted."""
        return len(ReviewStore.list_proposals(self))


def _counted(name: str):
    method = getattr(ReviewStore, name)

    def counted(self, *args, **kwargs):
        self.log.request(f"store:{name}")
        result = method(self, *args, **kwargs)
        if name == "list_proposals":
            self.log.request("store-rows:list_proposals", len(result))
        return result

    counted.__name__ = counted.__qualname__ = name
    counted.__doc__ = method.__doc__
    return counted


# the public calls a CountingReviewStore counts
STORE_CALLS = (
    "get_mention",
    "observe",
    "remap",
    "get_proposal",
    "list_proposals",
    "propose",
    "get_decision",
    "decide",
    "decide_many",
    "project",
    "pin_projection",
    "propose_claim",
    "get_claim_proposal",
    "get_claim_decision",
    "decide_claims",
    "pin_application",
)
for _name in STORE_CALLS:
    setattr(CountingReviewStore, _name, _counted(_name))


@dataclass
class Operation:
    """One measured call: its kind, the register it served, what it sent
    and wrote, and the graph's size before it."""

    operation: str
    source: int
    state: dict[str, int]
    requests: Counter = field(default_factory=Counter)
    writes: Counter = field(default_factory=Counter)
    seconds: float | None = None

    def report(self) -> dict:
        return {
            "operation": self.operation,
            "source": self.source,
            "state": self.state,
            "requests": dict(sorted(self.requests.items())),
            "writes": dict(sorted(self.writes.items())),
            **({"seconds": self.seconds} if self.seconds is not None else {}),
        }


def _delta(after: Counter, before: Counter) -> Counter:
    return Counter(
        {
            key: after[key] - before[key]
            for key in after
            if after[key] != before[key]
        }
    )


@dataclass
class CohortRun:
    """What `run_cohort` measured."""

    cohort: Cohort
    operations: list[Operation] = field(default_factory=list)
    synchronizations: int = 0
    sync_seconds: float = 0.0
    setup: dict[str, int] = field(default_factory=dict)


class _Recorder:
    def __init__(self, log, clock, setup_entities):
        self.log, self.clock = log, clock
        self.entities = setup_entities
        self.store = None
        self.claim_proposals = 0

    def state(self) -> dict[str, int]:
        _, writes = self.log.totals()
        return {
            "entities": self.entities + writes["item-create"],
            "bindings": writes["page:binding-index"],
            "authorizations": writes["page:authorization"],
            "commits": writes["page:assignment-commit"],
            "application_plans": writes["page:application-plan"],
            "proposals": self.store.size() if self.store else 0,
            "claim_proposals": self.claim_proposals,
        }

    @contextmanager
    def operation(self, run, name, source) -> Iterator[Operation]:
        record = Operation(name, source, self.state())
        requests, writes = self.log.totals()
        started = self.clock() if self.clock else None
        with self.log.phase(name):
            yield record
        if started is not None:
            record.seconds = self.clock() - started
        after_requests, after_writes = self.log.totals()
        record.requests = _delta(after_requests, requests)
        record.writes = _delta(after_writes, writes)
        run.operations.append(record)


def _approve(store, review) -> None:
    for proposal in review.proposals:
        previous = store.get_decision(proposal.id)
        if (
            previous is not None
            and previous.proposal.digest == proposal.digest
        ):
            continue
        store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            expected_decision_revision=previous.revision if previous else None,
            outcome="approve",
            actor=ACTOR,
            time=WHEN,
        )


def run_cohort(
    saved: tuple[SavedSource, ...],
    *,
    cohort: Cohort,
    client,
    workspace: Path,
    log: RequestLog,
    synchronize: Callable[[], None],
    clock: Callable[[], float] | None = None,
    setup_entities: int = 0,
) -> CohortRun:
    """Take every register of `saved` through registration, identities and
    claims with `client`, in order, recording each operation in `log`.

    `synchronize` waits until the query service has indexed every write;
    `clock`, when given, times each operation and each wait. The graph's
    size before each operation counts the `setup_entities` the graph held
    at the start and the records the run wrote since."""
    run = CohortRun(cohort=cohort)
    recorder = _Recorder(log, clock, setup_entities)
    exclusions = workspace / "never-ingest.txt"
    workspace.mkdir(parents=True, exist_ok=True)
    exclusions.write_text("# Explicitly empty for repository fiction.\n")
    entities: dict[str, object] = {}
    with log.phase("setup"):
        # an addition needs the deployment generation a fresh instance's
        # first registry record establishes
        CreationRegistry(client).ensure_generation()

    def wait() -> None:
        started = clock() if clock else None
        with log.phase("synchronize"):
            synchronize()
        run.synchronizations += 1
        if started is not None:
            run.sync_seconds += clock() - started

    with CountingReviewStore(workspace / "review.sqlite", log) as store:
        recorder.store = store
        for entry in saved:
            source = entry.source
            number = source.number
            workflow = SourceIngestion(
                entry.path,
                workspace / f"source-{number:04d}",
                client=client,
                review_store=store,
                exclusions_file=exclusions,
                document_dir=entry.document_dir,
            )
            with recorder.operation(run, "registration", number):
                workflow.prepare()
                proposal = workflow.prepare_registration(
                    ARTIFACT_TYPE, source.title
                )
                workflow.register_source(
                    proposal, expected_digest=proposal.digest, actor=ACTOR
                )
            choices = tuple(
                ReviewProposal(
                    operation="assign" if mention.shared else "new_entity",
                    mentions=(
                        MentionKey(
                            artifact_sha1=entry.sha1,
                            local_id=mention.local_id,
                        ),
                    ),
                    target=entities[mention.person.key]
                    if mention.shared
                    else None,
                )
                for mention in source.mentions
            )
            wait()
            with recorder.operation(run, "identity-review", number):
                prepared = workflow.prepare_identities(choices)
                _approve(store, prepared.review)
            for mention in source.mentions:
                wait()
                with recorder.operation(run, "identity-apply", number):
                    applied = workflow.apply_identities((mention.local_id,))
                mention_id = prepared.review.mentions[
                    entry.sha1, mention.local_id
                ].id
                entity_id = applied.acceptance.snapshot.assignments[
                    mention_id
                ].entity_id
                targets = {
                    binding.entity_id: binding
                    for binding in applied.request.current_targets
                }
                entities.setdefault(mention.person.key, targets[entity_id])
            if source.claims:
                indices = tuple(range(len(source.claims)))
                with recorder.operation(run, "claim-review", number):
                    review = workflow.prepare_claims(indices)
                    record_claim_decisions(
                        store,
                        review,
                        [
                            ClaimDecisionInput(
                                proposal_id=claim.proposal.id,
                                expected_digest=claim.proposal.digest,
                                outcome="approve",
                                actor=ACTOR,
                                time=WHEN,
                            )
                            for claim in review.claims
                        ],
                    )
                recorder.claim_proposals += len(review.claims)
                with recorder.operation(run, "claim-apply", number):
                    written = workflow.apply_claims(indices)
                if len(written.receipts) != len(source.claims):
                    raise RuntimeError("a reviewed claim was not applied")
    return run


# -- reports ------------------------------------------------------------------

# The phases whose operations one identity and one claim cost: reviewing a
# register's identity choices and applying each, and reviewing and applying
# a register's claims. Registration is a register's own cost.
IDENTITY = ("identity-review", "identity-apply")
CLAIM = ("claim-review", "claim-apply")
SCAN = ("entity-scan", "entity-scan-page", "entity-scan-batch")
ITEM_READS = ("item-read", "statement-read", "entity-read")
WRITES = ("page-write", "item-create", "item-edit", "upload")


def categories(requests: Counter) -> dict[str, int | float]:
    """The request counts `requests` gives by kind, gathered as the
    measured reads and writes are named: whole-instance entity scans and
    the requests they take, revision batches, immutable page reads (two
    requests each), item reads, writes, query-service queries, every other
    server request, and the review-store calls and proposals read."""
    server = {k: v for k, v in requests.items() if not k.startswith("store")}
    named = {
        "entity scans": requests["entity-scan"],
        "entity scan requests": sum(requests[k] for k in SCAN),
        "revision reads": requests["revision-read"],
        "immutable page reads": requests["page-read"] / 2,
        "item reads": sum(requests[k] for k in ITEM_READS),
        "writes": sum(requests[k] for k in WRITES),
        "query-service queries": sum(
            v for k, v in server.items() if k.startswith("query:")
        ),
    }
    counted = set(SCAN) | set(ITEM_READS) | set(WRITES)
    counted |= {"revision-read", "page-read"}
    named["other requests"] = sum(
        v
        for k, v in server.items()
        if k not in counted and not k.startswith("query:")
    )
    named["server requests"] = sum(server.values())
    named["review-store calls"] = sum(
        v for k, v in requests.items() if k.startswith("store:")
    )
    named["proposals read"] = requests["store-rows:list_proposals"]
    return named


def _totals(operations) -> dict:
    requests, writes, seconds = Counter(), Counter(), 0.0
    timed = True
    for operation in operations:
        requests.update(operation.requests)
        writes.update(operation.writes)
        if operation.seconds is None:
            timed = False
        else:
            seconds += operation.seconds
    return {
        "operations": len(operations),
        "requests": dict(sorted(requests.items())),
        "writes": dict(sorted(writes.items())),
        "categories": categories(requests),
        **({"seconds": seconds} if timed and operations else {}),
    }


def _per(totals: dict, count: int) -> dict:
    if not count:
        return {}
    return {
        "count": count,
        "requests": {k: v / count for k, v in totals["requests"].items()},
        "writes": {k: v / count for k, v in totals["writes"].items()},
        "categories": {k: v / count for k, v in totals["categories"].items()},
        **(
            {"seconds": totals["seconds"] / count}
            if "seconds" in totals
            else {}
        ),
    }


def report(run: CohortRun) -> dict:
    """The run's measurements: every operation, each phase's totals, and
    the mean cost of one identity, one claim and one registration."""
    cohort = run.cohort
    phases = {}
    for name in ("registration", *IDENTITY, *CLAIM):
        phases[name] = _totals(
            [op for op in run.operations if op.operation == name]
        )
    identity = _totals(
        [op for op in run.operations if op.operation in IDENTITY]
    )
    claim = _totals([op for op in run.operations if op.operation in CLAIM])
    found = {
        "cohort": cohort.describe(),
        "setup": run.setup,
        "phases": phases,
        "per_identity": _per(identity, cohort.identities),
        "per_claim": _per(claim, cohort.claims),
        "per_registration": _per(phases["registration"], len(cohort.sources)),
        "synchronizations": run.synchronizations,
        "operations": [operation.report() for operation in run.operations],
    }
    if any(op.seconds is not None for op in run.operations):
        found["sync_seconds"] = run.sync_seconds
        found["sync_seconds_per_identity"] = (
            run.sync_seconds / cohort.identities
        )
    return found


def operation_counts(found: dict) -> list[dict]:
    """Each operation of a report as the regression baseline keeps it: its
    kind, register and request and write counts, without times."""
    return [
        {
            key: operation[key]
            for key in ("operation", "source", "requests", "writes")
        }
        for operation in found["operations"]
    ]


def measure(
    scale: Scale,
    directory: Path,
    *,
    clock: Callable[[], float] | None = None,
    trace: bool = False,
) -> dict:
    """The report of one run of `scale`'s cohort against a fresh
    `FakeWikibase`, set up as a fictional-lane stack is: bootstrapped
    through the production bootstrap, with the existing people and the
    deployment generation. Products and workspaces are made in
    `directory`; `clock`, when given, times every operation. With `trace`,
    the report's "senders" give each operation kind's requests by kind and
    by the chain of yizkr functions that sent them."""
    from tests.fixtures import fake_wikibase

    from yizkr.core.wiki import WikiClient

    cohort = generate(scale)
    saved = saved_products(cohort, directory / "products")
    wiki = fake_wikibase.FakeWikibase()
    wiki.log.trace = trace
    with fake_wikibase.serving(wiki):
        wiki.bootstrap()
        client = WikiClient(fake_wikibase.config())
        with wiki.log.phase("setup"):
            # established before any entity outside the vocabulary
            CreationRegistry(client).ensure_generation()
            add_existing(client, cohort)
        wiki.query_service.synchronize()
        setup = {
            "entities": len(wiki.entities),
            "requests": dict(sorted(wiki.log.requests["setup"].items())),
        }
        run = run_cohort(
            saved,
            cohort=cohort,
            client=client,
            workspace=directory / "workspace",
            log=wiki.log,
            synchronize=wiki.query_service.synchronize,
            clock=clock,
        )
    run.setup = setup
    found = report(run)
    if trace:
        found["senders"] = [
            {"phase": phase, "kind": kind, "path": list(path), "count": n}
            for (phase, kind, path), n in wiki.log.paths.most_common()
        ]
    return found
