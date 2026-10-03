"""Backup/export against the running stack.

Needs docker + the deploy/ stack up (make up). Produces a real backup
into a temp dir and verifies the non-privileged export's privacy
invariant the hard way: no excluded person may be mentioned anywhere — by
QID, via another person's claim, or through provenance-node content.
"""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

import pytest

from yizkr.backup import run_backup

pytestmark = pytest.mark.live

QID_TOKEN = re.compile(r"Q[1-9]\d*\b")
REFERENCE_NODE = re.compile(r"<[^<>]*/reference/[0-9a-f]{40}>")


def test_backup_and_nonprivileged_export_privacy(config, tmp_path):
    if not Path("deploy/docker-compose.yml").is_file():
        pytest.skip("run from the repo root")
    manifest = run_backup(
        config,
        backup_root=tmp_path,
        deploy_dir=Path("deploy"),
        keep=0,
    )
    out_dir = tmp_path / manifest["timestamp"]
    for component in ("database", "media", "rdf", "nonprivileged_export"):
        assert manifest["components"][component]["bytes"] > 0

    exclusions = json.loads(
        (out_dir / "nonprivileged-exclusions.json").read_text()
    )
    excluded = set(exclusions["excluded"])
    with gzip.open(
        out_dir / "nonprivileged.nt.gz", "rt", encoding="utf-8"
    ) as handle:
        public = handle.read()

    # the invariant: nothing in the non-privileged export mentions an excluded
    # person — not their QID, not a claim about them, not a provenance
    # node quoting a source line that names them
    mentioned = {
        token
        for line in public.splitlines()
        for token in QID_TOKEN.findall(line)
    }
    assert not (mentioned & excluded)
    # an excerpt (a raw source quotation) leaves only with a statement it
    # attests (#363; owner, 2026-09-29): every reference node left is one
    # that a statement left derives from, so no dropped statement's excerpt
    # stays behind. A reference node's IRI ends in its SHA-1; the
    # reference properties' own IRIs (…/prop/reference/Pn, which the dump
    # declares for every property) are no nodes
    triples = [
        parts
        for line in public.splitlines()
        if len(parts := line.split(" ", 2)) == 3
    ]
    derived = {
        rest.rsplit(" ", 1)[0]
        for _, predicate, rest in triples
        if predicate == "<http://www.w3.org/ns/prov#wasDerivedFrom>"
    }
    references = {
        subject
        for subject, _, _ in triples
        if REFERENCE_NODE.fullmatch(subject)
    }
    assert references and references <= derived
    # people with recorded deaths are present
    assert exclusions["included_persons"]
    assert any(
        f"entity/{qid}>" in public for qid in exclusions["included_persons"]
    )
