"""Live validation of deaths and births on the person (ADR-0040; #687),
through the write path, the privacy check, the site and the
non-privileged export.

A fictional fixture from the example universe (docs/EXAMPLES.md) writes:

* Cäcilie's date of death of unknown value, as the 1905 marriage
  certificate's "dessen verstorbener Ehefrau Cäcilie" states it, beside
  the dated one her 1902 death register gives;
* a hypothetical "der selige Felix": Felix's date of death of unknown
  value alone, which releases him; and the same for a person whose one
  such statement a curator deprecated, who stays possibly living;
* a curator's "no value" date of death, which releases no one, and is
  never matched against an unknown value, either way;
* Eugen's age at death, "73 Jahre", alone, which releases him (Q1);
* the 1958 letter's "вдова Вашего покойного брата Рудольфа": Nina
  widowed and her marriage to Rudolf `ended_by_death_of` him, which
  releases Rudolf alone (Q9): the export leaves the union out, since Nina
  is possibly living, and keeps Rudolf;
* the 1950 adoption order's Alfred, `father` → Leopold with `parentage`
  → `parentage_adoptive` and "Adoptivvater" its word.

The first write is made again and writes no copy: the writer compares
snak types, so an unknown value finds its own statement and never a
curator's "no value". A curator-made death event still releases its
principal (Q5): test_live_event_privacy.py covers it.

Each source is registered with a fictional PNG of its own
(register_fictional), and every item's label carries the session's
marker (fixture_mark). Like every live test it writes fictional data to
the one instance, so the session that runs it ends by clearing the
recorded instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import gzip
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from wikibaseintegrator.wbi_enums import WikibaseRank, WikibaseSnakType

from tests.fixtures.identity import write_mapped
from yizkr import staticsite
from yizkr.backup import dump_rdf, exclusion_report, export_nonprivileged
from yizkr.claims import ClaimBatch
from yizkr.core import privacy
from yizkr.pipeline import writer
from yizkr.pipeline.writer import BatchWriter
from yizkr.staticsite import PEOPLE_QUERY, SiteBuilder, publish_site

from .conftest import fixture_mark, register_fictional, wait_for_query_service

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#687: Cäcilie's death of unknown date beside her dated one; Felix's "
    "alone; one a curator deprecated; a curator's no value; Eugen's age "
    "at death alone; Nina widowed and her marriage ended by Rudolf's "
    "death; Alfred's adoptive father"
)
MARK = fixture_mark(FIXTURE, writer, privacy, staticsite)
DEPLOY = Path("deploy")

# the sources' own words (docs/EXAMPLES.md)
LATE_WIFE = "dessen verstorbener Ehefrau Cäcilie"
REGISTER = "Ehefrau des Hutmachermeisters Eugen Löbig"
LATE_FELIX = "der selige Felix"
AGED = "73 Jahre"
WIDOW = "вдова Вашего покойного брата Рудольфа"
ADOPTED = "Adoptivvater"


def local(key: str, label: str, kind: str = "human") -> dict:
    return {
        "kind": "local",
        "local_id": f"deaths-{key}",
        "label": f"{label} ({MARK})",
        "instance_of": kind,
    }


ENTITIES = {
    "caecilie": local("caecilie", "Cäcilie Löbig"),
    "felix": local("felix", "Felix Löbig"),
    "deprecated": local("deprecated", "Felix Löbig, deprecated"),
    "no value": local("no-value", "Nicholas Lobig"),
    "eugen": local("eugen", "Eugen Löbig"),
    "nina": local("nina", "Nina Löbig"),
    "rudolf": local("rudolf", "Rudolf Löbig"),
    "marriage": local("marriage", "Marriage of Rudolf and Nina", "marriage"),
    "alfred": local("alfred", "Alfred Löbig"),
    "leopold": local("leopold", "Leopold Löbig"),
}
UNKNOWN = {"type": "unknown"}


def item(value: dict) -> dict:
    return {"type": "item", "value": value}


def vocab(key: str) -> dict:
    return item({"kind": "vocab", "key": key})


def claim(subject, prop, value, source, excerpt, language, qualifiers=()):
    return {
        "subject": ENTITIES[subject],
        "property": prop,
        "value": value,
        "qualifiers": list(qualifiers),
        "source": {
            "artifact": source.model_dump(mode="json"),
            "page_number": "1",
            "transcription_excerpt": {"text": excerpt, "language": language},
        },
    }


def batch(sources) -> ClaimBatch:
    certificate, register, burial, letter, order = sources
    spouse = [{"property": "object_has_role", "value": vocab("spouse")}]
    claims = [
        claim(
            "caecilie", "date_of_death", UNKNOWN, certificate, LATE_WIFE, "de"
        ),
        claim(
            "caecilie",
            "date_of_death",
            {"type": "time", "year": 1902, "month": 12, "day": 13},
            register,
            REGISTER,
            "de",
        ),
        claim("felix", "date_of_death", UNKNOWN, burial, LATE_FELIX, "de"),
        claim(
            "deprecated", "date_of_death", UNKNOWN, burial, LATE_FELIX, "de"
        ),
        # Nicholas, whom the curator's "no value" is about, is made by a
        # claim of his own, which states nothing of a death
        claim(
            "no value",
            "religion",
            {
                "type": "monolingual",
                "value": {"text": "lutherisch", "language": "de"},
            },
            burial,
            "lutherisch",
            "de",
        ),
        claim(
            "eugen",
            "age_at_death",
            {"type": "string", "value": AGED},
            burial,
            AGED,
            "de",
        ),
        claim("nina", "civil_status", vocab("widowed"), letter, WIDOW, "ru"),
        claim(
            "marriage",
            "participant",
            item(ENTITIES["nina"]),
            letter,
            WIDOW,
            "ru",
            spouse,
        ),
        claim(
            "marriage",
            "participant",
            item(ENTITIES["rudolf"]),
            letter,
            WIDOW,
            "ru",
            spouse,
        ),
        claim(
            "marriage",
            "ended_by_death_of",
            item(ENTITIES["rudolf"]),
            letter,
            WIDOW,
            "ru",
        ),
        claim(
            "alfred",
            "father",
            item(ENTITIES["leopold"]),
            order,
            ADOPTED,
            "de",
            [
                {
                    "property": "parentage",
                    "value": vocab("parentage_adoptive"),
                },
                {
                    "property": "kinship_type",
                    "value": {"type": "string", "value": ADOPTED},
                },
            ],
        ),
    ]
    return ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "integration-test",
                "pipeline_version": "0.0.0",
                "started_at": datetime.now(UTC).isoformat(),
            },
            "claims": claims,
        }
    )


def deaths_of(client, qid: str) -> list:
    properties, _ = client.vocabulary()
    entity = client.wbi.item.get(qid)
    return entity.claims.get(properties["date_of_death"])


def deprecate_death(client, qid: str) -> None:
    """A curator's deprecated rank on the date of death of `qid`: an
    optional overlay (ADR-0002). Idempotent."""
    properties, _ = client.vocabulary()
    entity = client.wbi.item.get(qid)
    (statement,) = entity.claims.get(properties["date_of_death"])
    if statement.rank != WikibaseRank.DEPRECATED:
        statement.rank = WikibaseRank.DEPRECATED
        entity.write(summary="integration test: curator rank on a fixture")


def add_no_value(client, qid: str, artifact_qid: str) -> None:
    """A curator's "no value" date of death on `qid`, stated in the
    artifact `artifact_qid` (rule 1, ADR-0003): no pipeline write makes
    one, so it is added here. Idempotent."""
    from wikibaseintegrator.datatypes import Item, String, Time
    from wikibaseintegrator.models import Reference, References
    from wikibaseintegrator.wbi_enums import ActionIfExists

    properties, _ = client.vocabulary()
    entity = client.wbi.item.get(qid)
    none = WikibaseSnakType.NO_VALUE
    if any(
        statement.mainsnak.snaktype == none
        for statement in entity.claims.get(properties["date_of_death"])
    ):
        return
    reference = Reference()
    reference.add(Item(prop_nr=properties["stated_in"], value=artifact_qid))
    reference.add(String(prop_nr=properties["page_number"], value="1"))
    references = References()
    references.add(reference)
    entity.claims.add(
        Time(
            prop_nr=properties["date_of_death"],
            snaktype=none,
            references=references,
        ),
        action_if_exists=ActionIfExists.FORCE_APPEND,
    )
    entity.write(summary="integration test: a curator's no value")


@pytest.fixture(scope="module")
def fixture_graph(client, config):
    """Write the fixture, again (which must write nothing), and the
    curator's overlays, and wait until the query service holds every
    fixture item's latest revision. Returns the QIDs by key, the sources'
    under "certificate", "register", "burial", "letter" and "order"."""
    registered = {
        key: register_fictional(client, f"Deaths fixture {key} {MARK}", kind)
        for key, kind in (
            ("certificate", "certificate"),
            ("register", "certificate"),
            ("burial", "certificate"),
            ("letter", "letter"),
            ("order", "certificate"),
        )
    }
    sources = [each.ref for each in registered.values()]
    written = write_mapped(BatchWriter(client), batch(sources))
    qids = {
        key: written.created_items[ref["local_id"]]
        for key, ref in ENTITIES.items()
    }
    deprecate_death(client, qids["deprecated"])
    add_no_value(client, qids["no value"], registered["burial"].qid)
    again = write_mapped(BatchWriter(client), batch(sources))
    assert (again.written, again.refused) == (0, [])
    assert again.created_items == written.created_items
    qids |= {key: each.qid for key, each in registered.items()}
    wait_for_query_service(config, client, list(qids.values()))
    return qids


def test_an_unknown_value_is_a_somevalue_snak_written_once(
    client, fixture_graph
):
    (statement,) = deaths_of(client, fixture_graph["felix"])
    assert statement.mainsnak.snaktype == WikibaseSnakType.UNKNOWN_VALUE
    assert statement.rank == WikibaseRank.NORMAL
    # Cäcilie's two, the unknown and the dated, coexist
    snaks = sorted(
        s.mainsnak.snaktype.value
        for s in deaths_of(client, fixture_graph["caecilie"])
    )
    assert snaks == ["somevalue", "value"]


def test_an_unknown_value_never_matches_a_no_value(client, config):
    """The writer writes an unknown value onto a person who holds only a
    curator's "no value": a statement of its own, the two never one."""
    source = register_fictional(
        client, f"Deaths fixture no-value source {MARK}", "certificate"
    )
    person = {
        "kind": "local",
        "local_id": "deaths-no-value-then-unknown",
        "label": f"Agnes Löbig, then the late ({MARK})",
        "instance_of": "human",
    }
    first = write_mapped(
        BatchWriter(client),
        ClaimBatch.model_validate(
            {
                "extraction_run": {
                    "model_identifier": "integration-test",
                    "pipeline_version": "0.0.0",
                    "started_at": datetime.now(UTC).isoformat(),
                },
                "claims": [
                    {
                        "subject": person,
                        "property": "religion",
                        "value": {
                            "type": "monolingual",
                            "value": {"text": "lutherisch", "language": "de"},
                        },
                        "source": {
                            "artifact": source.ref.model_dump(mode="json"),
                            "transcription_excerpt": {
                                "text": "lutherisch",
                                "language": "de",
                            },
                        },
                    }
                ],
            }
        ),
    )
    qid = first.created_items[person["local_id"]]
    add_no_value(client, qid, source.qid)
    unknown = ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "integration-test",
                "pipeline_version": "0.0.0",
                "started_at": datetime.now(UTC).isoformat(),
            },
            "claims": [
                {
                    "subject": person,
                    "property": "date_of_death",
                    "value": UNKNOWN,
                    "source": {
                        "artifact": source.ref.model_dump(mode="json"),
                        "transcription_excerpt": {
                            "text": LATE_FELIX,
                            "language": "de",
                        },
                    },
                }
            ],
        }
    )
    written = write_mapped(BatchWriter(client), unknown)
    assert written.written == 1
    assert sorted(
        s.mainsnak.snaktype.value for s in deaths_of(client, qid)
    ) == ["novalue", "somevalue"]
    # and again: the unknown value finds its own statement
    again = write_mapped(BatchWriter(client), unknown)
    assert again.written == 0
    assert len(deaths_of(client, qid)) == 2


def test_the_check_releases_by_an_unknown_value_and_an_age(
    config, fixture_graph
):
    report = exclusion_report(config)
    for key in ("caecilie", "felix", "eugen", "rudolf"):
        assert fixture_graph[key] in report.included, key
    # Eugen's age at death is his only death evidence: the check reads it
    # through the facts query's own arm (backup.PERSON_FACTS_QUERY,
    # _DEATH_BINDINGS), Q1
    assert report.reasons[fixture_graph["eugen"]] == "death recorded"
    # a deprecated unknown value, and a curator's "no value", release no
    # one; nor does a widowhood
    for key in ("deprecated", "no value", "nina"):
        assert fixture_graph[key] in report.excluded, key


def roster(config, tmp_path) -> tuple[SiteBuilder, dict[str, dict]]:
    builder = SiteBuilder(config, tmp_path / "roster")
    people = {
        row["person"]["value"].rsplit("/", 1)[-1]: row
        for row in builder.run(builder._instance_query(PEOPLE_QUERY))
    }
    return builder, people


def test_the_site_reads_them_as_the_check_does(
    config, fixture_graph, tmp_path
):
    builder, people = roster(config, tmp_path)
    # ?deceased reads an unknown value and an age at death alike (Q1)
    for key in ("caecilie", "felix", "eugen"):
        assert people[fixture_graph[key]]["deceased"]["value"] == "true", key
    for key in ("deprecated", "no value", "rudolf", "nina"):
        assert people[fixture_graph[key]]["deceased"]["value"] == "false", key
    report = exclusion_report(config)
    badge = "possibly living — never in the non-privileged export"
    for key in ("caecilie", "felix", "deprecated", "no value", "eugen"):
        qid = fixture_graph[key]
        badged = badge in builder.person_page(people[qid], [])
        assert badged == (qid in report.excluded), key
    # Rudolf, released by the union's end alone, in the badge too (Q9)
    assert badge not in builder.person_page(
        people[fixture_graph["rudolf"]], []
    )
    assert badge in builder.person_page(people[fixture_graph["nina"]], [])


@pytest.fixture(scope="module")
def site(config, fixture_graph, tmp_path_factory):
    out = tmp_path_factory.mktemp("deaths-site")
    try:
        yield out, publish_site(config, out)
    finally:
        # the site renders the whole instance, not only this fixture
        shutil.rmtree(out, ignore_errors=True)


def test_the_person_page_shows_it_beside_the_dated_one(fixture_graph, site):
    out, summary = site
    qid = fixture_graph["caecilie"]
    page = (out / f"people/{qid}.html").read_text()
    (deaths,) = re.findall(r"<dt>date of death</dt>((?:<dd>.*?</dd>)+)", page)
    unknown = (
        'died (date not stated)<span class="source">'
        f'<a href="../artifacts/{fixture_graph["certificate"]}.html">'
    )
    assert unknown in deaths
    assert "1902-12-13" in deaths
    assert f"../artifacts/{fixture_graph['register']}.html" in deaths
    assert "genid" not in page
    assert summary["unknown_deaths"] >= 2
    # the run record's releases by cause count them (ADR-0040 §7)
    releases = summary["privacy"]["releases"]
    assert releases["unknown_date_of_death"] >= 2
    assert releases["age_at_death"] >= 1
    assert releases["ended_by_death_of"] >= 1


def test_the_timeline_shows_it_beside_the_dated_one(fixture_graph, site):
    out, _ = site
    timeline = (out / "timeline.html").read_text()
    person = f'href="people/{fixture_graph["caecilie"]}.html"'
    entries = [
        entry
        for entry in re.findall(r'<div class="entry">(.*?)</div>', timeline)
        if person in entry
    ]
    (beside,) = [e for e in entries if "died (date not stated)" in e]
    (dated,) = [e for e in entries if "1902-12-13" in e]
    assert entries.index(beside) == entries.index(dated) + 1
    # Felix's, dated by nothing, stands under "No date stated"
    foot = timeline[timeline.index("<h2>No date stated</h2>") :]
    assert f'href="people/{fixture_graph["felix"]}.html"' in foot
    assert person not in foot


def test_an_adoptive_father_shows_its_kind(fixture_graph, site):
    out, _ = site
    page = (out / f"people/{fixture_graph['alfred']}.html").read_text()
    assert "parentage: adoptive" in page
    assert f"kinship type: {ADOPTED}" in page


@pytest.fixture(scope="module")
def exported(config, fixture_graph, tmp_path_factory):
    """The dump and its non-privileged export, in a directory of their own."""
    if not (DEPLOY / "docker-compose.yml").is_file():
        pytest.skip("run from the repo root")
    out_dir = tmp_path_factory.mktemp("deaths-export")
    dump_rdf(DEPLOY, out_dir / "full.nt.gz")
    export_nonprivileged(config, out_dir)
    return out_dir


def test_the_export_keeps_whom_they_release(fixture_graph, exported):
    exclusions = json.loads(
        (exported / "nonprivileged-exclusions.json").read_text()
    )
    with gzip.open(exported / "nonprivileged.nt.gz", "rt") as dump:
        text = dump.read()

    def mentioned(key: str) -> bool:
        return re.search(rf"/{fixture_graph[key]}>", text) is not None

    for key in ("caecilie", "felix", "eugen", "rudolf"):
        assert fixture_graph[key] in exclusions["included_persons"], key
        assert mentioned(key), key
    for key in ("deprecated", "no value", "nina"):
        assert fixture_graph[key] in exclusions["excluded"], key
        assert not mentioned(key), key
    # the union of a possibly-living partner is left out, and the
    # released partner it names stays
    assert fixture_graph["marriage"] in exclusions["excluded"]
    assert not mentioned("marriage")
