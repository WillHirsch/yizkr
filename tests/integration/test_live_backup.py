"""Backup/export (W1 task 7) against the running stack.

Needs docker + the deploy/ stack up (make up). Produces a real backup
into a temp dir and verifies the public export's privacy invariant the
hard way: no excluded person may be mentioned anywhere — by QID, via
another person's claim, or through provenance-node content.
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


def test_backup_and_public_export_privacy(config, tmp_path):
    if not Path("deploy/docker-compose.yml").is_file():
        pytest.skip("run from the repo root")
    manifest = run_backup(
        config,
        backup_root=tmp_path,
        deploy_dir=Path("deploy"),
        keep=0,
    )
    out_dir = tmp_path / manifest["timestamp"]
    for component in ("database", "media", "rdf", "public_export"):
        assert manifest["components"][component]["bytes"] > 0

    exclusions = json.loads((out_dir / "public-exclusions.json").read_text())
    excluded = set(exclusions["excluded"])
    with gzip.open(out_dir / "public.nt.gz", "rt", encoding="utf-8") as handle:
        public = handle.read()

    # the invariant: nothing in the public export mentions an excluded
    # person — not their QID, not a claim about them, not a provenance
    # node quoting a source line that names them
    mentioned = {
        token
        for line in public.splitlines()
        for token in QID_TOKEN.findall(line)
    }
    assert not (mentioned & excluded)
    # excerpts (raw source quotations) never enter the public tier
    from yizkr.core.wiki import WikiClient

    properties, _ = WikiClient(config).vocabulary()
    excerpt_marker = f"/prop/reference/{properties['transcription_excerpt']}>"
    assert not any(
        line.split(" ", 2)[1].endswith(excerpt_marker)
        for line in public.splitlines()
        if line.count(" ") >= 2
    )
    # people with recorded deaths are present
    assert exclusions["included_persons"]
    assert any(
        f"entity/{qid}>" in public for qid in exclusions["included_persons"]
    )
