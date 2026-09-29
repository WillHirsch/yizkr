"""yizkr — one CLI for every tool in the family-archive system.

    yizkr bootstrap        apply model/manifest.yaml to the instance
    yizkr smoke-test       verify a deployment end to end
    yizkr publish-queries  publish the SPARQL library to wiki pages
    yizkr claims           claim-contract utilities (JSON schema, validate)
    yizkr ingest <step>    pipeline steps; every step reads/writes plain
                           files so runs pause for review and resume
    yizkr stages state     what a run did, from its run directory
    yizkr stages manifest  what shapes a run, from its run.json
    yizkr scratch run      the whole corpus through every stage, unattended;
                           run it again to resume
    yizkr scratch estimate the engine calls that run would make, per stage,
                           counted without making one
    yizkr scratch watch    follow a scratch run; pause|resume|abort ask its
                           driver to stop or go on
    yizkr scratch report   what a scratch run did, per stage and per
                           document, from its run directory alone

Connection settings come from deploy/.env (or $YIZKR_ENV_FILE /
--env-file); see deploy/README.md.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from yizkr.claims import ClaimBatch
from yizkr.core.config import Config
from yizkr.core.privacy import DEFAULT_CUTOFF_YEARS
from yizkr.core.wiki import WikiClient
from yizkr.pipeline.orchestrate import MODEL_ROLES

# where `ingest run` keeps the LLM cache unless told otherwise
DEFAULT_CACHE = Path("var/llm-cache")


def _client(args) -> WikiClient:
    return WikiClient(Config.load(args.env_file))


def _load_batch(path: Path) -> ClaimBatch:
    return ClaimBatch.model_validate_json(path.read_text())


def _refuse_if_excluded(
    args, path: Path | None = None, *, sha1: str | None = None
) -> None:
    """Honour the never-ingest list in a single-file step.

    The single steps are debugging tools and run without a list; when one
    is configured, a file on it is refused before anything reads it. A
    step whose input is not the source file, as `ingest extract` reads a
    transcription, gives the content SHA-1 its input records, and the
    source file too when it is given one. Without a file only the list's
    SHA-1 entries can be checked, so a list that names files by pattern
    fails closed. The extract-all command, `ingest run`, requires the
    list (cmd_run)."""
    from yizkr.core.corpus import is_excluded, load_exclusions

    if path is None and sha1 is None:
        return
    exclusions = load_exclusions(required=False, env_file=args.env_file)
    if exclusions is None:
        return
    if is_excluded(exclusions, path, sha1=sha1):
        what = path.name if path is not None else f"SHA-1 {sha1[:12]}"
        sys.exit(f"{what} is on the never-ingest list; refusing")
    if path is None and exclusions.patterns:
        sys.exit(
            "the never-ingest list names files by pattern, and no file was "
            "given to match them against: give the transcribed media file "
            "(--media); refusing"
        )


# -- top-level commands -------------------------------------------------


def cmd_bootstrap(args) -> int:
    from yizkr.model.bootstrap import bootstrap_instance
    from yizkr.model.schema import load_manifest

    manifest = (
        load_manifest(args.manifest) if args.manifest else load_manifest()
    )
    config = Config.load(args.env_file)
    print(f"Wikibase: {config.wikibase_url}")
    bootstrap_instance(WikiClient(config), manifest, dry_run=args.dry_run)
    return 0


def cmd_smoke_test(args) -> int:
    from yizkr.smoke import run_smoke

    run_smoke(Config.load(args.env_file), timeout=args.timeout)
    return 0


def cmd_backup(args) -> int:
    from yizkr.backup import QueryServiceLag, run_backup

    try:
        manifest = run_backup(
            Config.load(args.env_file),
            backup_root=args.out,
            deploy_dir=args.deploy_dir,
            compose_files=args.compose_file or None,
            skip_media=args.skip_media,
            public_export=not args.no_public_export,
            cutoff_years=args.cutoff_years,
            keep=args.keep,
        )
    except QueryServiceLag as refusal:
        # the dumps it kept, then the refusal on one line, for cron's log
        if refusal.manifest is not None:
            _print_backup(args, refusal.manifest)
        print(f"public export refused: {refusal}", file=sys.stderr)
        return 1
    _print_backup(args, manifest)
    return 0


def _print_backup(args, manifest: dict) -> None:
    for name, info in manifest["components"].items():
        detail = "".join(
            f", {key}={value}" for key, value in info.items() if key != "file"
        )
        print(
            f"{name}: "
            f"{args.out / manifest['timestamp'] / info['file']}{detail}"
        )
    if manifest["pruned"]:
        print(f"pruned old backups: {', '.join(manifest['pruned'])}")


def cmd_merge_items(args) -> int:
    client = _client(args)
    for qid in (args.from_qid, args.to_qid):
        if not client.fetch_entities([qid]).get(qid):
            sys.exit(f"{qid} does not exist")
    client.merge_items(args.from_qid, args.to_qid, summary=args.summary)
    print(
        f"merged {args.from_qid} into {args.to_qid}; "
        f"{args.from_qid} is now a redirect"
    )
    return 0


def cmd_publish_queries(args) -> int:
    from yizkr.queries.publish import publish_queries

    config = Config.load(args.env_file)
    publish_queries(WikiClient(config), config, dry_run=args.dry_run)
    return 0


def cmd_reconcile_places(args) -> int:
    from yizkr.pipeline.places import (
        adjudicate_places,
        apply_containment,
        build_worklist,
        comma_parent_candidates,
        export_places_csv,
        fetch_competing_pairs,
        fetch_places,
    )

    config = Config.load(args.env_file)
    client = WikiClient(config)
    if args.apply:
        summary = apply_containment(
            client, config, args.apply, "curator-reviewed"
        )
        for note in summary["notes"]:
            print(f"note: {note}", file=sys.stderr)
        print(
            f"containment written: {summary['written']}, already present: "
            f"{summary['skipped']}"
            + (
                f", run item {summary['run_qid']}"
                if summary["run_qid"]
                else ""
            )
        )
        remaining = len(fetch_competing_pairs(client, config))
        print(
            f"{remaining} competing place pair(s) still unresolved — re-run "
            f"`yizkr reconcile-places` to derive the next (smaller) worklist."
        )
        return 0

    from yizkr.pipeline.llm import make_client

    places = fetch_places(client, config)
    competing = fetch_competing_pairs(client, config)
    comma = comma_parent_candidates(places)
    worklist = build_worklist(places, competing, comma)
    print(
        f"places: {len(places)}; unresolved competing pairs: "
        f"{len(competing)}; comma-parent candidates: {len(comma)}; "
        f"questions after transitive elimination + dedupe: {len(worklist)}"
    )
    if not worklist:
        print(
            "nothing to adjudicate — the hierarchy already answers everything"
        )
        return 0
    proposals = adjudicate_places(make_client(args.llm), worklist, places)
    rows = export_places_csv(proposals, worklist, places, args.out)
    print(
        f"wrote {args.out} ({rows} proposals). Fill the 'decision' column, "
        f"then re-run with --apply. Containment rows write referenced "
        f"part_of statements; 'same' rows stay curator-executed."
    )
    return 0


def cmd_publish_site(args) -> int:
    from yizkr.staticsite import publish_site

    summary = publish_site(
        Config.load(args.env_file), args.out, clean=args.clean
    )
    print(json.dumps(summary, indent=2))
    print(
        "NOTE: built from the LIVE graph — includes possibly-living "
        "people; serve on the LAN only "
        "(e.g. `python -m http.server -d {out}`).".format(out=summary["out"])
    )
    return 0


def cmd_claims(args) -> int:
    from pydantic import ValidationError

    from yizkr.claims import validate_against_manifest

    if args.json_schema:
        print(json.dumps(ClaimBatch.model_json_schema(), indent=2))
        return 0
    try:
        batch = ClaimBatch.model_validate_json(args.validate.read_text())
    except ValidationError as exc:
        print(f"Schema errors in {args.validate}:\n{exc}", file=sys.stderr)
        return 1
    problems = validate_against_manifest(batch)
    for problem in problems:
        print(f"manifest check: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(
        f"{args.validate}: valid ({len(batch.claims)} claims, "
        f"run {batch.extraction_run.model_identifier})"
    )
    return 0


# -- ingest steps --------------------------------------------------------


def cmd_register(args) -> int:
    from yizkr.pipeline.registrar import register_artifact

    _refuse_if_excluded(args, args.file)
    result = register_artifact(
        _client(args),
        media_path=args.file,
        artifact_type=args.type,
        title=args.title,
        description=args.description,
        pages=args.pages,
    )
    print(json.dumps(result.__dict__, indent=2))
    return 0


def cmd_transcribe(args) -> int:
    _refuse_if_excluded(args, args.file)
    if args.engine == "textfile":
        if not args.text_file:
            sys.exit("--engine textfile requires --text-file")
        from yizkr.pipeline.transcribe import TextFileTranscriber

        transcriber = TextFileTranscriber(
            args.text_file,
            language=args.language,
            fallback_language=args.fallback_language,
        )
    elif args.engine == "llm":
        from yizkr.pipeline.llm import make_client
        from yizkr.pipeline.transcribe import (
            LlmVisionTranscriber,
            MissingPrerequisite,
            preflight_vision_transcription,
        )

        try:
            preflight_vision_transcription([args.file])
        except MissingPrerequisite as exc:
            sys.exit(str(exc))
        transcriber = LlmVisionTranscriber(
            make_client(args.llm),
            language=args.language,
            fallback_language=args.fallback_language,
        )
    else:  # htr
        from yizkr.pipeline.transcribe import HtrPlatformAdapter

        transcriber = HtrPlatformAdapter()
    transcription = transcriber.transcribe(args.file)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(transcription.model_dump_json(indent=2))
    print(
        f"wrote {args.out} ({len(transcription.text)} chars, "
        f"engine {transcription.engine})"
    )
    return 0


def cmd_extract(args) -> int:
    from pydantic import ValidationError

    from yizkr.claims import ArtifactRef
    from yizkr.pipeline.transcribe import Transcription

    try:
        transcription = Transcription.model_validate_json(
            args.transcription.read_text()
        )
    except ValidationError as exc:
        found = "; ".join(
            f"{'.'.join(map(str, error['loc'])) or 'file'}: {error['msg']}"
            for error in exc.errors()[:3]
        )
        sys.exit(
            f"{args.transcription.name} is not a transcription this step "
            f"reads ({found}): transcribe the media again with `yizkr ingest "
            f"transcribe`"
        )
    # the transcription names the artifact it transcribes by the SHA-1 of
    # its media, the key its claims cite: the list is asked for that key,
    # and for the media file by name and hash when one is given
    _refuse_if_excluded(args, args.media, sha1=transcription.artifact.sha1)
    if args.media is not None and (
        ArtifactRef.from_path(args.media) != transcription.artifact
    ):
        sys.exit(
            f"{args.media.name} is not the media {args.transcription.name} "
            f"transcribes (its SHA-1 differs); refusing"
        )
    if args.engine == "file":
        if not args.claims_file:
            sys.exit("--engine file requires --claims-file")
        from yizkr.pipeline.extract import ClaimFileExtractor

        extractor = ClaimFileExtractor(args.claims_file)
    else:
        from yizkr.pipeline.chunking import (
            AUTO_CHUNK_THRESHOLD,
            ChunkedClaimExtractor,
        )
        from yizkr.pipeline.extract import LlmClaimExtractor
        from yizkr.pipeline.llm import cached_client, make_client, open_cache

        client = make_client(args.llm)
        if args.cache is not None:
            client = cached_client(
                client, open_cache(args.cache), role="extract"
            )
        chunked = args.strategy == "chunked" or (
            args.strategy == "auto"
            and len(transcription.text) > AUTO_CHUNK_THRESHOLD
        )
        if chunked:
            extractor = ChunkedClaimExtractor(
                client,
                state_dir=args.out.with_name(args.out.name + ".chunks"),
                budget=args.chunk_budget,
                retries=args.chunk_retries,
                concurrency=args.chunk_concurrency,
            )
        else:
            raw_dump = args.out.with_name(args.out.name + ".raw.txt")
            rejects_dump = args.out.with_name(args.out.name + ".rejected.json")
            extractor = LlmClaimExtractor(
                client,
                raw_dump=raw_dump,
                rejects_dump=rejects_dump,
            )
    batch = extractor.extract(transcription, transcription.artifact)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(batch.model_dump_json(indent=2))
    print(f"wrote {args.out} ({len(batch.claims)} claims)")
    rejects_path = getattr(extractor, "rejects_dump", None)
    if rejects_path is not None and rejects_path.exists():
        rejected = len(json.loads(rejects_path.read_text()))
        print(
            f"warning: {rejected} claims failed validation and were NOT "
            f"written; reasons in {rejects_path}",
            file=sys.stderr,
        )
    failed = getattr(extractor, "failed_chunks", None)
    if failed:
        print(
            f"warning: {len(failed)} chunks failed after retries "
            f"({', '.join(failed)}); batch is PARTIAL — re-run to retry "
            f"them (with --cache DIR, the calls that completed are "
            f"answered from the cache)",
            file=sys.stderr,
        )
        return 3
    return 0


def cmd_resolve(args) -> int:
    from yizkr.pipeline.resolve import resolve_batch

    batch = _load_batch(args.batch)
    if args.equivalences:
        from yizkr.pipeline.reconcile import (
            apply_equivalences,
            read_approved_equivalences,
            read_reviewed_aliases,
        )

        approved = read_approved_equivalences(args.equivalences)
        batch = apply_equivalences(
            batch, approved, read_reviewed_aliases(args.equivalences)
        )
        print(f"applied {len(approved)} approved equivalences")
    _, item_labels = _client(args).label_index()
    resolved = resolve_batch(batch, item_labels)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(resolved.model_dump_json(indent=2))
    print(f"wrote {args.out}")
    return 0


def cmd_gedcom(args) -> int:
    from yizkr.claims import ArtifactRef
    from yizkr.pipeline.gedcom import adapt_gedcom, format_unsupported_tags

    _refuse_if_excluded(args, args.file)
    result = adapt_gedcom(
        args.file.read_text(), artifact=ArtifactRef.from_path(args.file)
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(result.batch.model_dump_json(indent=2))
    print(
        f"wrote {args.out} ({len(result.batch.claims)} claims, "
        f"{len(result.batch.entities)} entities) — deterministic, no LLM; "
        f"flows through reconcile/review/write as usual"
    )
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if result.unsupported_tags:
        print(
            "unsupported GEDCOM tags (counted, not extracted): "
            + format_unsupported_tags(result.unsupported_tags),
            file=sys.stderr,
        )
    return 0


def cmd_reconcile(args) -> int:
    from yizkr.pipeline.llm import make_client
    from yizkr.pipeline.reconcile import (
        adjudicate,
        block_candidates,
        export_equivalences_csv,
        fetch_roster,
    )

    batch = _load_batch(args.batch)
    if not batch.entities:
        sys.exit(
            f"{args.batch} carries no entity manifest; reconcile needs a "
            f"batch from entity-first extraction (ADR-0007)"
        )
    config = Config.load(args.env_file)
    roster = fetch_roster(WikiClient(config), config)
    candidates = block_candidates(batch.entities, roster)
    print(
        f"roster: {len(roster)} graph entities; candidates for "
        f"{len(candidates)} of {len(batch.entities)} document entities"
    )
    proposals = adjudicate(
        make_client(args.llm), batch.entities, candidates, batch
    )
    rows = export_equivalences_csv(proposals, batch.entities, roster, args.out)
    print(
        f"wrote {args.out} ({rows} proposals). Fill the 'decision' column "
        f"with approve/reject, then pass --equivalences to resolve. "
        f"Nothing takes effect without your approval."
    )
    return 0


def cmd_reconcile_graph(args) -> int:
    from yizkr.pipeline.llm import make_client
    from yizkr.pipeline.reconcile import (
        export_graph_pairs_csv,
        export_place_containment_csv,
        fetch_place_containment,
        fetch_roster,
        place_containment_proposals,
        propose_graph_pairs,
    )

    config = Config.load(args.env_file)
    client = WikiClient(config)
    roster = fetch_roster(client, config)
    containment = fetch_place_containment(client, config)
    proposals = propose_graph_pairs(
        make_client(args.llm), roster, containment=containment
    )
    rows = export_graph_pairs_csv(proposals, roster, args.out)
    print(
        f"wrote {args.out} ({rows} pairs from a {len(roster)}-entity "
        f"roster). Approved pairs are executed BY YOU: merge the items "
        f"on the wiki, or add a referenced 'said to be the same as' "
        f"statement where the equivalence is itself a judgement."
    )
    # hand-off (ADR-0017): distinct pairs the adjudicator flagged as one
    # containing the other become part_of proposals for reconcile-places.
    containment_rows = place_containment_proposals(proposals, roster)
    if containment_rows:
        out = args.out.with_name(args.out.stem + "-containment.csv")
        export_place_containment_csv(containment_rows, out)
        print(
            f"wrote {out} ({len(containment_rows)} containment proposal(s) "
            f"discovered while rejecting equivalences). Review, then apply "
            f"with `yizkr reconcile-places --apply {out}` to write the "
            f"part_of edges — which then prune these pairs from future runs."
        )
    return 0


def cmd_export_review(args) -> int:
    from yizkr.pipeline.review import export_review_csv

    args.out.parent.mkdir(parents=True, exist_ok=True)
    rows = export_review_csv(_load_batch(args.batch), args.out)
    print(
        f"wrote {args.out} ({rows} rows). Fill 'decision' (approve/reject) "
        f"and optionally 'correction' (unambiguous replacement value), then "
        f"run apply-review."
    )
    return 0


def cmd_apply_review(args) -> int:
    from yizkr.pipeline.review import apply_review

    approved, warnings = apply_review(_load_batch(args.batch), args.csv)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(approved.model_dump_json(indent=2))
    print(f"wrote {args.out} ({len(approved.claims)} approved claims)")
    return 0


def cmd_accept_all(args) -> int:
    from yizkr.pipeline.review import accept_all

    # default output name follows the input: review.csv -> review.reviewed.csv,
    # equivalences.csv -> equivalences.reviewed.csv (the gate each satisfies).
    out = args.out or args.csv.with_name(args.csv.stem + ".reviewed.csv")
    if out.exists() and not args.force:
        sys.exit(f"{out} already exists; pass --force to overwrite it")
    try:
        result = accept_all(args.csv, out)
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))
    if result.mode == "equivalences":
        summary = (
            f"{result.approved} match(es) approved, "
            f"{result.rejected} distinct rejected, "
            f"{result.uncertain} uncertain left blank"
        )
        if result.conflicted:
            summary += (
                f", {result.conflicted} conflicting (same vs >1 item) left "
                f"blank — resolve these by hand"
            )
        print(f"wrote {out} (ratified {result.total} verdicts: {summary})")
    else:
        print(
            f"wrote {out} ({result.approved} claims set to bulk-accept; "
            f"the write path marks these low-scrutiny for reappraisal)"
        )
    return 0


def cmd_write(args) -> int:
    from yizkr.pipeline.writer import BatchWriter

    # a batch file that does not load is refused before the instance is
    # contacted
    batch = _load_batch(args.batch)
    result = BatchWriter(_client(args), dry_run=args.dry_run).write_batch(
        batch
    )
    print(json.dumps(result.__dict__, indent=2))
    if args.dry_run:
        print("(dry run: nothing written)")
    return 0


def cmd_run(args) -> int:
    from yizkr.core.corpus import CORPUS_DIR_VAR, corpus_dir, load_exclusions
    from yizkr.pipeline.orchestrate import Orchestrator
    from yizkr.pipeline.rundir import ModelAliasRefused
    from yizkr.pipeline.transcribe import MissingPrerequisite

    if args.no_cache and (args.refresh or args.epoch):
        sys.exit(
            "--refresh and --epoch act through the LLM cache, and --no-cache "
            "runs without one: drop --no-cache (a refreshed role's calls "
            "then go to the engine whatever the cache holds)"
        )
    # fail closed: no never-ingest list, no run, before any file is
    # read. An empty list file is an explicit, allowed configuration.
    exclusions = load_exclusions(required=True, env_file=args.env_file)
    folder = args.folder or corpus_dir(args.env_file)
    if folder is None:
        sys.exit(
            f"no folder given and {CORPUS_DIR_VAR} is not set; name the "
            f"folder, or set {CORPUS_DIR_VAR} (see deploy/README.md)"
        )
    if not folder.is_dir():
        sys.exit(f"{folder} is not a directory")
    state_dir = args.state or Path("var/runs") / folder.resolve().name
    orchestrator = Orchestrator(
        folder,
        state_dir,
        llm=args.llm,
        artifact_type=args.type,
        language=args.language,
        fallback_language=args.fallback_language,
        chunk_concurrency=args.chunk_concurrency,
        extract_effort=args.extract_effort,
        review_effort=args.review_effort,
        # "inherit" (or empty) means: don't override — use the main model
        extract_model=(args.extract_model or None)
        if args.extract_model != "inherit"
        else None,
        serial=args.serial,
        extract_concurrency=args.extract_concurrency,
        env_file=args.env_file,
        exclusions=exclusions,
        cache_dir=None if args.no_cache else args.cache,
        refresh=args.refresh or (),
        epochs=dict(args.epoch or ()),
    )
    try:
        return orchestrator.run()
    except (MissingPrerequisite, ModelAliasRefused) as exc:
        sys.exit(str(exc))


def _gibibytes(value: str) -> float:
    """A size in GiB for --min-free: a finite number, 0 or more."""
    try:
        size = float(value)
    except ValueError:
        size = math.nan
    if not (math.isfinite(size) and size >= 0):
        raise argparse.ArgumentTypeError(
            f"a size in GiB, finite and 0 or more, not {value!r}"
        )
    return size


def _role_epoch(value: str) -> tuple[str, int]:
    """`ROLE=N` -> (role, N), for --epoch."""
    role, _, epoch = value.partition("=")
    if role not in MODEL_ROLES or not epoch.isdigit():
        raise argparse.ArgumentTypeError(
            f"expected ROLE=N with ROLE one of {', '.join(MODEL_ROLES)} and "
            f"N a whole number, not {value!r}"
        )
    return role, int(epoch)


def cmd_stages_state(args) -> int:
    from yizkr.pipeline.rundir import RunDir, cache_counts, run_state

    run = RunDir(args.run)
    if not run.manifest():
        sys.exit(f"{args.run}: no run.json; is it a run directory?")
    states = run_state(run)
    ordered = sorted(states.items())
    cache = cache_counts(run)
    if args.json:
        rows = [
            {
                "subject": subject,
                "stage": stage,
                "status": state.status,
                "reason": state.reason,
            }
            for (subject, stage), state in ordered
        ]
        print(json.dumps({"stages": rows, "cache": cache}, indent=2))
        return 0
    summary: dict[str, dict[str, int]] = {}
    for (_subject, stage), state in ordered:
        counts = summary.setdefault(stage, {})
        counts[state.status] = counts.get(state.status, 0) + 1
    for stage, counts in summary.items():
        rendered = ", ".join(
            f"{count} {label}" for label, count in sorted(counts.items())
        )
        print(f"{stage:16} {rendered}")
    for role, counts in sorted(cache.items()):
        rendered = ", ".join(
            f"{count} {label}" for label, count in sorted(counts.items())
        )
        print(f"cache {role:10} {rendered} (all invocations)")
    return 0


def cmd_stages_manifest(args) -> int:
    from yizkr.pipeline.rundir import RunDir, describe_manifest

    manifest = RunDir(args.run).manifest()
    if not manifest:
        sys.exit(f"{args.run}: no run.json; is it a run directory?")
    for line in describe_manifest(manifest):
        print(line)
    return 0


def _scratch_plan(args) -> tuple[Path, dict]:
    """(the corpus folder, the driver's options) for the run a `scratch
    run` or `scratch estimate` command plans; exits when there is no
    never-ingest list or no corpus folder."""
    from yizkr.core.corpus import CORPUS_DIR_VAR, corpus_dir, load_exclusions

    # fail closed: no never-ingest list, no run, before any file is read
    exclusions = load_exclusions(required=True, env_file=args.env_file)
    folder = args.corpus or corpus_dir(args.env_file)
    if folder is None:
        sys.exit(
            f"no corpus given and {CORPUS_DIR_VAR} is not set; name it with "
            f"--corpus, or set {CORPUS_DIR_VAR} (see deploy/README.md)"
        )
    if not folder.is_dir():
        sys.exit(f"{folder} is not a directory")
    return folder, {
        "runs": args.runs,
        "resume": args.resume,
        "new": args.new,
        "until": args.until,
        "refresh": args.refresh or (),
        "cache_dir": args.cache,
        "env_file": args.env_file,
        "exclusions": exclusions,
        "language": args.language,
        "fallback_language": args.fallback_language,
        "chunk_concurrency": args.chunk_concurrency,
        "extract_effort": args.extract_effort,
        "review_effort": args.review_effort,
        "extract_model": (
            None
            if args.extract_model in ("", "inherit")
            else args.extract_model
        ),
        "min_free": int(args.min_free * 1024**3),
    }


def cmd_scratch_run(args) -> int:
    from yizkr.pipeline import scratch
    from yizkr.pipeline.llm import PassPaused
    from yizkr.pipeline.rundir import RunSettingsConflict

    folder, options = _scratch_plan(args)
    try:
        return scratch.scratch_run(folder, **options)
    except RunSettingsConflict as exc:
        print(
            f"{exc}\nA new scratch run: yizkr scratch run --new",
            file=sys.stderr,
        )
        return scratch.EXIT_FAILED
    except PassPaused as exc:
        # the engine's own account of the pause, and what to do
        print(str(exc), file=sys.stderr)
        return scratch.EXIT_STOPPED
    except scratch.ScratchRefused as exc:
        print(str(exc), file=sys.stderr)
        return scratch.EXIT_FAILED
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return scratch.EXIT_INTERRUPTED


def cmd_scratch_estimate(args) -> int:
    from yizkr.pipeline import estimate, scratch
    from yizkr.pipeline.llm import PassPaused
    from yizkr.pipeline.rundir import RunSettingsConflict

    folder, options = _scratch_plan(args)
    try:
        counted = estimate.scratch_estimate(folder, **options)
    except RunSettingsConflict as exc:
        print(
            f"{exc}\nThe run would be refused, before any call. Estimate a "
            "new run instead: yizkr scratch estimate --new",
            file=sys.stderr,
        )
        return scratch.EXIT_FAILED
    except (PassPaused, scratch.ScratchRefused) as exc:
        # the engine's own account of what stops it, or the refusal
        print(str(exc), file=sys.stderr)
        return scratch.EXIT_FAILED
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return scratch.EXIT_INTERRUPTED
    for line in estimate.describe(counted):
        print(line)
    return scratch.EXIT_OK


def _scratch_run_dir(args) -> Path:
    """The run a `scratch` command names (a run directory, or a run id in
    --runs), or the latest; exits when there is none."""
    from yizkr.pipeline import scratch

    try:
        return scratch.resolve_run(args.runs, args.run)
    except scratch.ScratchRefused as exc:
        sys.exit(str(exc))


def cmd_scratch_watch(args) -> int:
    from yizkr.pipeline import monitor

    if args.interval <= 0:
        sys.exit("--interval must be positive")
    if args.serve is not None:
        if args.no_page:
            sys.exit("--serve needs the page: drop --no-page")
        try:
            monitor.parse_address(args.serve)
        except ValueError as exc:
            sys.exit(str(exc))
    run = _scratch_run_dir(args)
    try:
        return monitor.watch(
            run,
            interval=args.interval,
            once=args.once,
            page=not args.no_page,
            serve=args.serve,
        )
    except OSError as exc:
        sys.exit(str(exc))


def cmd_scratch_report(args) -> int:
    from yizkr.pipeline import report

    run = _scratch_run_dir(args)
    try:
        written = report.write_report(run)
    except report.ReportError as exc:
        sys.exit(str(exc))
    for name in (report.MARKDOWN_FILE, report.HTML_FILE, report.JSON_FILE):
        print(f"{name:12} {written[name]}")
    print(
        f"{report.MARKDOWN_FILE} holds the aggregates alone, and may be "
        f"posted; {report.HTML_FILE} and {report.JSON_FILE} stay local"
    )
    return 0


def cmd_scratch_control(args) -> int:
    from yizkr.pipeline import monitor
    from yizkr.pipeline.rundir import ABORT, RESUME, RunDir

    run = _scratch_run_dir(args)
    again = f"yizkr scratch run --runs {run.parent} --resume {run.name}"
    scope = (
        f"stage:{args.stage.strip()}"
        if args.stage
        else f"kind:{args.kind.strip()}"
        if args.kind
        else "run"
    )
    if args.action == RESUME:
        try:
            aborted = RunDir(run).control().scopes.get(scope) == "aborted"
        except (OSError, TypeError, ValueError):
            aborted = False
        if aborted:
            print(
                f"{scope} of run {run.name} is aborted for the rest of this "
                "invocation, and a resume does not lift an abort: run "
                f"`{again}` to invoke the run again"
            )
            return 1
    try:
        entry = monitor.request(
            run,
            args.action,
            stage=args.stage,
            kind=args.kind,
            reason=args.reason,
            by="operator",
        )
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))
    written = entry["scope"]
    note = monitor.unknown_slice_note(written, monitor.summarise_run(run))
    print(
        f"{args.action} requested for {written} of run {run.name}: "
        f"{monitor.state_now(run, written)}{note}"
    )
    if args.action == ABORT:
        print(f"completed work is kept; `{again}` invokes the run again")
    return 0


def cmd_scratch_import_transcriptions(args) -> int:
    from yizkr.core.corpus import CORPUS_DIR_VAR, corpus_dir, load_exclusions
    from yizkr.pipeline import import_transcriptions as imports
    from yizkr.pipeline.llm import PassPaused
    from yizkr.pipeline.scratch import EXIT_FAILED, EXIT_INTERRUPTED

    # fail closed: no never-ingest list, nothing read
    exclusions = load_exclusions(required=True, env_file=args.env_file)
    folder = args.corpus or corpus_dir(args.env_file)
    if folder is None:
        sys.exit(
            f"no corpus given and {CORPUS_DIR_VAR} is not set; name it with "
            f"--corpus, or set {CORPUS_DIR_VAR} (see deploy/README.md)"
        )
    try:
        report = imports.import_transcriptions(
            args.archive,
            folder,
            cache_dir=args.cache,
            exclusions=exclusions,
            extract_effort=args.extract_effort,
            leave_out=args.leave_out or (),
        )
    except (imports.ImportRefused, PassPaused) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    for line in imports.describe(report):
        print(line)
    return 0


def cmd_detect_faces(args) -> int:
    from yizkr.pipeline.faces import Gallery, make_detector, proposals_to_batch

    _refuse_if_excluded(args, args.file)
    detector = make_detector(args.engine)
    detection = detector.detect(args.file, artifact_qid=args.artifact)
    args.detections.write_text(detection.model_dump_json(indent=2))
    gallery = Gallery.load(args.gallery)
    batch, unmatched = proposals_to_batch(detection, gallery, args.threshold)
    args.out.write_text(batch.model_dump_json(indent=2))
    print(f"detected {len(detection.faces)} faces -> {args.detections}")
    print(
        f"wrote {args.out} ({len(batch.claims)} depicts proposals; "
        f"{len(unmatched)} unmatched faces: {unmatched or 'none'})"
    )
    if unmatched:
        print("identify unmatched faces with enroll-face, then match-faces.")
    return 0


def _detections(path: Path):
    """The detections saved at `path`; a file without the photograph's
    SHA-1 is refused, with the fix."""
    from yizkr.pipeline.faces import UnkeyedDetections, load_detections

    try:
        return load_detections(path)
    except UnkeyedDetections as exc:
        sys.exit(str(exc))


def cmd_match_faces(args) -> int:
    from yizkr.pipeline.faces import Gallery, proposals_to_batch

    detection = _detections(args.detections)
    batch, unmatched = proposals_to_batch(
        detection, Gallery.load(args.gallery), args.threshold
    )
    args.out.write_text(batch.model_dump_json(indent=2))
    print(
        f"wrote {args.out} ({len(batch.claims)} depicts proposals; "
        f"{len(unmatched)} unmatched faces: {unmatched or 'none'})"
    )
    return 0


def cmd_enroll_face(args) -> int:
    from yizkr.pipeline.faces import Gallery, enroll_from_detections

    gallery = Gallery.load(args.gallery)
    person = enroll_from_detections(
        _detections(args.detections),
        args.face,
        gallery,
        label=args.label,
        qid=args.qid,
    )
    gallery.save(args.gallery)
    print(
        f"enrolled face {args.face} as {person.label} "
        f"({person.qid or 'no QID yet'}); "
        f"{len(person.embeddings)} embedding(s) in gallery"
    )
    return 0


# -- parser wiring --------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yizkr", description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="env file for connection settings "
        "(default: $YIZKR_ENV_FILE, deploy/.env, .env)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "bootstrap", help="apply the model manifest to the instance"
    )
    p.add_argument("--manifest", type=Path, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("smoke-test", help="verify a deployment end to end")
    p.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="seconds to wait for the WDQS round-trip",
    )
    p.set_defaults(func=cmd_smoke_test)

    p = sub.add_parser(
        "publish-queries", help="publish the SPARQL library to wiki pages"
    )
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_publish_queries)

    p = sub.add_parser(
        "merge-items",
        help="merge one item into another (curator action; one explicit "
        "pair per invocation — ADR-0007)",
    )
    p.add_argument("from_qid", metavar="FROM_QID")
    p.add_argument("to_qid", metavar="TO_QID")
    p.add_argument(
        "--summary",
        default="curator: reviewed duplicate merge (ADR-0007)",
        help="edit summary recorded in item history",
    )
    p.set_defaults(func=cmd_merge_items)

    p = sub.add_parser(
        "backup",
        help="database + media + RDF dump, and a possibly-living-excluded "
        "public export (ADR-0020)",
    )
    p.add_argument("--out", type=Path, default=Path("var/backups"))
    p.add_argument("--deploy-dir", type=Path, default=Path("deploy"))
    p.add_argument(
        "--compose-file",
        action="append",
        metavar="FILE",
        help="compose file(s) relative to --deploy-dir (repeatable; "
        "default docker-compose.yml)",
    )
    p.add_argument("--skip-media", action="store_true")
    p.add_argument(
        "--no-public-export",
        action="store_true",
        help="skip the possibly-living-excluded export",
    )
    p.add_argument(
        "--cutoff-years",
        type=int,
        default=DEFAULT_CUTOFF_YEARS,
        metavar="N",
        help="without any death evidence, only people born more than N "
        f"years ago enter the public export (default {DEFAULT_CUTOFF_YEARS})",
    )
    p.add_argument(
        "--keep",
        type=int,
        default=0,
        metavar="N",
        help="prune to the newest N backups (0 = keep everything)",
    )
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser(
        "reconcile-graph",
        help="propose duplicate pairs among EXISTING items (ADR-0007); "
        "execution of approved pairs stays a curator action",
    )
    p.add_argument(
        "--llm", default="anthropic", choices=["anthropic", "claude-code"]
    )
    p.add_argument("--out", type=Path, default=Path("graph-equivalences.csv"))
    p.set_defaults(func=cmd_reconcile_graph)

    p = sub.add_parser(
        "reconcile-places",
        help="propose place containment for unresolved competing values "
        "(ADR-0009); approved edges write referenced part_of statements",
    )
    p.add_argument(
        "--llm", default="anthropic", choices=["anthropic", "claude-code"]
    )
    p.add_argument("--out", type=Path, default=Path("place-hierarchy.csv"))
    p.add_argument(
        "--apply",
        type=Path,
        help="reviewed CSV: write approved containment rows and report "
        "what transitive elimination removed",
    )
    p.set_defaults(func=cmd_reconcile_places)

    p = sub.add_parser(
        "publish-site",
        help="render the live graph to a static site (frontend ladder rung 2; "
        "LAN/private — includes possibly-living people)",
    )
    p.add_argument("--out", type=Path, default=Path("var/site"))
    p.add_argument(
        "--clean",
        action="store_true",
        help="remove the output directory first",
    )
    p.set_defaults(func=cmd_publish_site)

    p = sub.add_parser("claims", help="claim-contract utilities")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--json-schema",
        action="store_true",
        help="print the ClaimBatch JSON Schema",
    )
    group.add_argument(
        "--validate",
        type=Path,
        metavar="FILE",
        help="schema + manifest check a batch file",
    )
    p.set_defaults(func=cmd_claims)

    ingest = sub.add_parser("ingest", help="pipeline steps")
    isub = ingest.add_subparsers(dest="step", required=True)

    p = isub.add_parser(
        "register", help="upload media + artifact/fragment items"
    )
    p.add_argument(
        "--file",
        type=Path,
        required=True,
        help="the artifact's media file: the artifact is found or made by "
        "the SHA-1 of its bytes (ADR-0010)",
    )
    p.add_argument(
        "--type",
        required=True,
        help="manifest item key, e.g. photograph, certificate",
    )
    p.add_argument("--title", required=True)
    p.add_argument("--description")
    p.add_argument(
        "--pages", type=int, default=0, help="create N page-fragment items"
    )
    p.set_defaults(func=cmd_register)

    p = isub.add_parser("transcribe", help="media -> transcription JSON")
    p.add_argument(
        "--file",
        type=Path,
        required=True,
        help="the artifact's media file; the transcription names it by the "
        "SHA-1 of its bytes, which its claims cite",
    )
    p.add_argument(
        "--engine", choices=["llm", "textfile", "htr"], default="textfile"
    )
    p.add_argument(
        "--text-file",
        type=Path,
        help="existing transcription (textfile engine)",
    )
    p.add_argument(
        "--language",
        default="auto",
        help="document language (default 'auto' detects from the text); "
        "an explicit code like de forces it",
    )
    p.add_argument(
        "--fallback-language",
        default="en",
        help="language tag when auto-detection is inconclusive (default en)",
    )
    p.add_argument(
        "--llm",
        default="anthropic",
        choices=["anthropic", "claude-code"],
        help="LLM provider for --engine llm (explicit opt-in; sends "
        "artifact content to that provider). anthropic = API key, "
        "token billing; claude-code = CLI login, subscription billing",
    )
    p.add_argument("--out", type=Path, default=Path("transcription.json"))
    p.set_defaults(func=cmd_transcribe)

    p = isub.add_parser("extract", help="transcription -> claim batch JSON")
    p.add_argument(
        "--transcription",
        type=Path,
        required=True,
        help="an `ingest transcribe` output: the llm engine's claims cite "
        "the artifact it names by the SHA-1 of its media, which the write "
        "resolves to the item `ingest register` made for it (a claims "
        "file's claims cite their artifacts themselves)",
    )
    p.add_argument(
        "--media",
        type=Path,
        help="the transcribed media file, which must be the one the "
        "transcription names: the never-ingest list is checked against "
        "its name as well as its SHA-1. Required when the list names files "
        "by pattern, since a transcription keeps no file name",
    )
    p.add_argument("--engine", choices=["llm", "file"], default="file")
    p.add_argument(
        "--claims-file", type=Path, help="prepared claims JSON (file engine)"
    )
    p.add_argument(
        "--llm", default="anthropic", choices=["anthropic", "claude-code"]
    )
    p.add_argument("--out", type=Path, default=Path("batch.json"))
    p.add_argument(
        "--strategy",
        choices=["auto", "single", "chunked"],
        default="auto",
        help="chunked extraction (ADR-0006): auto picks chunked for "
        "large transcriptions; its coverage report, rejects and gaps go "
        "to <out>.chunks/",
    )
    p.add_argument("--chunk-budget", type=int, default=2000, metavar="CHARS")
    p.add_argument("--chunk-retries", type=int, default=2, metavar="N")
    p.add_argument(
        "--chunk-concurrency",
        type=int,
        default=1,
        metavar="N",
        help="run up to N chunk calls in parallel (chunks are "
        "independent; default 1 = sequential)",
    )
    p.add_argument(
        "--cache",
        type=Path,
        metavar="DIR",
        help="answer each model call from this LLM cache when the same "
        "call was answered before, and keep every new answer there "
        "(ADR-0032; `ingest run` uses var/llm-cache), so a re-run asks "
        "the engine only for what did not complete. Without it every "
        "call goes to the engine",
    )
    p.set_defaults(func=cmd_extract)

    p = isub.add_parser(
        "gedcom",
        help="deterministic GEDCOM export -> claim batch (no LLM)",
    )
    p.add_argument(
        "--file",
        type=Path,
        required=True,
        help="GEDCOM file (.ged); every claim cites it by the SHA-1 of its "
        "bytes, which the write resolves to the item `ingest register` "
        "made for it",
    )
    p.add_argument("--out", type=Path, default=Path("batch.json"))
    p.set_defaults(func=cmd_gedcom)

    p = isub.add_parser(
        "reconcile",
        help="propose entity equivalences against the graph (ADR-0007)",
    )
    p.add_argument("--batch", type=Path, required=True)
    p.add_argument(
        "--llm", default="anthropic", choices=["anthropic", "claude-code"]
    )
    p.add_argument("--out", type=Path, default=Path("equivalences.csv"))
    p.set_defaults(func=cmd_reconcile)

    p = isub.add_parser(
        "resolve", help="match local refs to existing items (exact label)"
    )
    p.add_argument("--batch", type=Path, required=True)
    p.add_argument(
        "--equivalences",
        type=Path,
        help="reviewed equivalences CSV from `ingest reconcile`; approved "
        "rows resolve to existing items before label matching",
    )
    p.add_argument("--out", type=Path, default=Path("resolved.json"))
    p.set_defaults(func=cmd_resolve)

    p = isub.add_parser("export-review", help="batch -> CSV for OpenRefine")
    p.add_argument("--batch", type=Path, required=True)
    p.add_argument("--out", type=Path, default=Path("review.csv"))
    p.set_defaults(func=cmd_export_review)

    p = isub.add_parser(
        "apply-review", help="filter batch by reviewed CSV decisions"
    )
    p.add_argument("--batch", type=Path, required=True)
    p.add_argument("--csv", type=Path, required=True)
    p.add_argument("--out", type=Path, default=Path("approved.json"))
    p.set_defaults(func=cmd_apply_review)

    p = isub.add_parser(
        "accept-all",
        help=(
            "bulk-accept a review CSV: approve every claim (review.csv), or "
            "ratify each machine verdict (equivalences.csv) — curator "
            "bulk-accept"
        ),
    )
    p.add_argument(
        "--csv",
        type=Path,
        required=True,
        help="the review.csv or equivalences.csv to accept",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output path (default: <input-stem>.reviewed.csv beside --csv)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing reviewed CSV",
    )
    p.set_defaults(func=cmd_accept_all)

    p = isub.add_parser("write", help="write approved claims as statements")
    p.add_argument("--batch", type=Path, required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_write)

    p = isub.add_parser(
        "run",
        help="walk a folder through the whole pipeline, pausing at the "
        "review gates; re-run to resume",
    )
    p.add_argument(
        "folder",
        type=Path,
        nargs="?",
        help="folder of documents to ingest (default $YIZKR_CORPUS_DIR). "
        "Refuses to run unless $YIZKR_EXCLUDE_FILE names a never-ingest "
        "list; see deploy/README.md",
    )
    p.add_argument(
        "--state",
        type=Path,
        help="state/output directory (default var/runs/<folder name>)",
    )
    p.add_argument(
        "--llm", default="anthropic", choices=["anthropic", "claude-code"]
    )
    p.add_argument(
        "--type",
        default="artifact",
        help="manifest item key for registered artifacts (default artifact)",
    )
    p.add_argument(
        "--language",
        default="auto",
        help="document language, e.g. de, en (tags source excerpts). "
        "default 'auto' detects it per file from the transcription — a "
        "mixed-language folder needs no per-file split; an explicit code "
        "forces it for every file",
    )
    p.add_argument(
        "--fallback-language",
        default="en",
        help="language tag for documents too short to auto-detect "
        "(default en); ignored when --language is an explicit code",
    )
    p.add_argument(
        "--chunk-concurrency",
        type=int,
        default=3,
        metavar="N",
        help="run up to N extraction chunks in parallel per document "
        "(chunks are independent; default 3). Lower it if the engine "
        "rate-limits; 1 is fully sequential",
    )
    p.add_argument(
        "--extract-concurrency",
        type=int,
        default=1,
        metavar="N",
        help="with --serial, extract N documents in parallel during the "
        "up-front extract-all phase (default 1). Multiplies with "
        "--chunk-concurrency (N x chunks concurrent LLM calls), so keep the "
        "product within the engine's limits. The phase reads and writes no "
        "wiki: each document is registered and written after it, one at a "
        "time. No effect without --serial",
    )
    effort_choices = ["low", "medium", "high", "xhigh", "max"]
    p.add_argument(
        "--extract-effort",
        choices=effort_choices,
        default="low",
        help="reasoning effort for the mechanical bulk steps "
        "(transcription, extraction). Default low — these are high-volume "
        "and deep thinking there is wasted (claude-code engine only)",
    )
    p.add_argument(
        "--review-effort",
        choices=effort_choices,
        default="high",
        help="reasoning effort for the judgment step, the extraction "
        "critique. Default high — a low-volume, high-stakes review worth "
        "the thinking (claude-code engine only)",
    )
    p.add_argument(
        "--extract-model",
        default="claude-sonnet-5",
        metavar="MODEL",
        help="model for the claim-extraction step only; the run's main "
        "model ($YIZKR_LLM_MODEL) still drives transcription and the "
        "critique. Default claude-sonnet-5 — extraction rewards "
        "source discipline over raw capability (ADR-0016). Pass 'inherit' "
        "to use the run's main model for extraction too. Either is a full "
        "model id: the run refuses an alias such as 'sonnet' or "
        "'...-latest' (docs/RUN_MANIFEST.md)",
    )
    p.add_argument(
        "--serial",
        action="store_true",
        help="take artifacts one at a time, hubs first, each fully written "
        "before the next reconciles — so a later artifact's resolve step "
        "finds, by exact label, the items the earlier ones wrote; "
        "reconcile-graph consolidates an entity two artifacts name "
        "differently (ADR-0019). Advance by re-running after each claim "
        "review; about one re-run per artifact",
    )
    cache = p.add_mutually_exclusive_group()
    cache.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_CACHE,
        metavar="DIR",
        help="the LLM cache, shared across runs (default var/llm-cache; "
        "ADR-0032): when a view is made again, each model call it makes is "
        "answered from the cache if the same call, keyed on what it sends, "
        "was answered before, and every new answer is kept there. It only "
        "saves engine calls: a document's views persist in its state "
        "directory and are reused while their inputs are unchanged, with "
        "or without it",
    )
    cache.add_argument(
        "--no-cache",
        action="store_true",
        help="run without the LLM cache: a view that is made again calls "
        "the engine for every call, and no answer is read or kept. Views "
        "persist and are reused as with the cache, so the run is otherwise "
        "the same",
    )
    p.add_argument(
        "--refresh",
        action="append",
        choices=MODEL_ROLES,
        metavar="ROLE",
        help="make this role's views again in this run, in place, current "
        "or not, asking the engine afresh for each call even where the "
        "cache holds an answer, which the new one replaces (repeatable; "
        f"one of {', '.join(MODEL_ROLES)}; extract includes the GEDCOM "
        "adapter's batches)",
    )
    p.add_argument(
        "--epoch",
        action="append",
        type=_role_epoch,
        metavar="ROLE=N",
        help="key this role's calls under epoch N (default 0), so that a "
        "view made again samples the role afresh while the answers of "
        "other epochs stay in the cache; current views stand, so combine "
        "it with --refresh to re-sample them (repeatable)",
    )
    p.set_defaults(func=cmd_run)

    p = isub.add_parser(
        "detect-faces", help="detect faces in a photo -> depicts proposals"
    )
    p.add_argument(
        "--file",
        type=Path,
        required=True,
        help="photo file; the depicts claims cite it by the SHA-1 of its "
        "bytes",
    )
    p.add_argument(
        "--artifact",
        required=True,
        help="QID of the registered photograph item, the depicts claims' "
        "subject",
    )
    p.add_argument("--engine", choices=["insightface"], default="insightface")
    p.add_argument("--gallery", type=Path, default=Path("gallery.json"))
    p.add_argument(
        "--threshold",
        type=float,
        default=0.4,
        help="cosine similarity cutoff for suggestions",
    )
    p.add_argument(
        "--detections",
        type=Path,
        default=Path("detections.json"),
        help="output: all faces + embeddings (for enroll-face)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("faces-batch.json"),
        help="output: claim batch for the review flow",
    )
    p.set_defaults(func=cmd_detect_faces)

    p = isub.add_parser(
        "match-faces", help="re-match saved detections against the gallery"
    )
    p.add_argument("--detections", type=Path, required=True)
    p.add_argument("--gallery", type=Path, default=Path("gallery.json"))
    p.add_argument("--threshold", type=float, default=0.4)
    p.add_argument("--out", type=Path, default=Path("faces-batch.json"))
    p.set_defaults(func=cmd_match_faces)

    p = isub.add_parser(
        "enroll-face", help="curator: state the identity of a detected face"
    )
    p.add_argument("--detections", type=Path, required=True)
    p.add_argument(
        "--face",
        type=int,
        required=True,
        help="face index within the detections file",
    )
    p.add_argument("--label", required=True, help="person's English label")
    p.add_argument("--qid", help="person item QID, if it exists")
    p.add_argument("--gallery", type=Path, default=Path("gallery.json"))
    p.set_defaults(func=cmd_enroll_face)

    stages = sub.add_parser(
        "stages",
        help="what a run did to each document's stages, and what shapes "
        "the run, from its run directory",
    )
    ssub = stages.add_subparsers(dest="action", required=True)

    p = ssub.add_parser(
        "state",
        help="per-stage run state, and what the LLM cache did for each "
        "role, derived from a run directory's events; the cache counts are "
        "totals over every invocation the run directory has seen",
    )
    p.add_argument("run", type=Path, help="run directory (with run.json)")
    p.add_argument(
        "--json",
        action="store_true",
        help="every document and stage, with its reason, and the cache "
        "counts per role",
    )
    p.set_defaults(func=cmd_stages_state)

    p = ssub.add_parser(
        "manifest",
        help="what shapes a run, from its run.json (docs/RUN_MANIFEST.md): "
        "mode and project stage, engine and CLI version, each role's "
        "model, effort, epoch and client configuration hash, the cache, "
        "the corpus and its never-ingest list's hash, the code version, "
        "and when it started and ended",
    )
    p.add_argument("run", type=Path, help="run directory (with run.json)")
    p.set_defaults(func=cmd_stages_manifest)

    _scratch_parsers(sub, effort_choices)
    return parser


def _scratch_parsers(sub, effort_choices) -> None:
    """`yizkr scratch`: the scratch driver and its call estimate
    (docs/runbooks/scratch-run.md), the import of archived transcriptions
    for its calls (docs/LLM_CACHE.md, Seeding), and the run monitor's
    commands (docs/MONITOR.md)."""
    from yizkr.pipeline import scratch
    from yizkr.pipeline.rundir import CONTROL_ACTIONS

    group = sub.add_parser(
        "scratch",
        help="whole-corpus scratch runs: import archived transcriptions for "
        "one's calls, estimate its calls, run it, follow it, pause, resume "
        "or abort it (docs/runbooks/scratch-run.md)",
    )
    ssub = group.add_subparsers(dest="action", required=True)
    runs_help = (
        f"the runs directory (default {scratch.DEFAULT_RUNS}); a run is "
        "named by its id there, or by its directory's path"
    )

    p = ssub.add_parser(
        "import-transcriptions",
        help="adopt an archive's vision transcriptions in the LLM cache, "
        "each checked, for the calls a scratch run makes, so the run "
        "reuses them rather than asks the engine (docs/LLM_CACHE.md, "
        "Seeding). Refuses to run unless $YIZKR_EXCLUDE_FILE names a "
        "never-ingest list",
    )
    p.add_argument(
        "archive",
        type=Path,
        help="the archived state directory of an `ingest run`: a folder "
        "per document, with its state.json and transcription.json",
    )
    p.add_argument(
        "--corpus",
        type=Path,
        help="the corpus folder the scratch run takes (default "
        "$YIZKR_CORPUS_DIR), which must hold each scan whose transcription "
        "is adopted",
    )
    p.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_CACHE,
        metavar="DIR",
        help="the LLM cache the scratch run takes (default var/llm-cache)",
    )
    p.add_argument(
        "--extract-effort",
        choices=effort_choices,
        default="low",
        help="the reasoning effort the scratch run transcribes with, as "
        "its --extract-effort (default low); the adoptions are keyed on it, "
        "and on $YIZKR_LLM_MODEL and the claude CLI installed",
    )
    p.add_argument(
        "--leave-out",
        action="append",
        metavar="SHA1",
        help="leave out the transcription of the media whose SHA-1 starts "
        "so (at least 7 hex digits), and withdraw an earlier adoption of "
        "it, so the run transcribes it afresh (repeatable)",
    )
    p.set_defaults(func=cmd_scratch_import_transcriptions)

    p = ssub.add_parser(
        "run",
        help="take the whole corpus through every stage, unattended: the "
        "per-document stages, the barrier, then the corpus stages. Runs the "
        "latest run again when it did not go to the end, so the same "
        "command resumes it; a new run otherwise",
    )
    _scratch_plan_options(p, effort_choices, runs_help)
    p.set_defaults(func=cmd_scratch_run)

    p = ssub.add_parser(
        "estimate",
        help="the engine calls the same `scratch run` would make now, per "
        "stage: those the LLM cache cannot answer, counted without a model "
        "call or a wiki read, and without holding what a running driver "
        "holds. Takes the options of `scratch run`",
        description="Plans the run `yizkr scratch run` with the same "
        "options would make now, a new one or the one it would invoke "
        "again, and counts the engine calls it would make, per stage and "
        "in all: the calls whose views do not stand that the LLM cache "
        "cannot answer. Known counts are keyed as the run keys its calls; "
        "the calls after an answer the engine has yet to give are "
        "estimated, and what only its answers decide is given as up to so "
        "many more, an estimate, not a ceiling. It "
        "makes no model call, reads no wiki, writes nothing and takes no "
        "lock, so it runs beside a running driver "
        "(docs/runbooks/scratch-run.md, Before a run). Takes the options "
        "of `scratch run`.",
    )
    _scratch_plan_options(p, effort_choices, runs_help)
    p.set_defaults(func=cmd_scratch_estimate)

    p = ssub.add_parser(
        "watch",
        help="follow a scratch run (the latest by default) in the live "
        "monitor (docs/MONITOR.md); quitting it never stops the run",
    )
    p.add_argument("run", nargs="?", help="a run id or directory")
    p.add_argument(
        "--runs", type=Path, default=scratch.DEFAULT_RUNS, help=runs_help
    )
    p.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="seconds between refreshes (default 2)",
    )
    p.add_argument("--once", action="store_true", help="print one view")
    p.add_argument(
        "--no-page",
        action="store_true",
        help="do not write the status page into the run directory",
    )
    p.add_argument(
        "--serve",
        metavar="ADDRESS:PORT",
        help="serve the status page, and nothing else, while the monitor "
        "runs (off by default)",
    )
    p.set_defaults(func=cmd_scratch_watch)

    p = ssub.add_parser(
        "report",
        help="what a scratch run (the latest by default) did, per stage and "
        "per document, read from its run directory alone: report.html and "
        "report.json, which stay local, and report.md, the aggregates "
        "alone, which may be posted (docs/RUN_REPORT.md)",
    )
    p.add_argument("run", nargs="?", help="a run id or directory")
    p.add_argument(
        "--runs", type=Path, default=scratch.DEFAULT_RUNS, help=runs_help
    )
    p.set_defaults(func=cmd_scratch_report)

    for action in CONTROL_ACTIONS:
        p = ssub.add_parser(
            action,
            help=f"ask a scratch run's driver to {action} the run, or one "
            "stage or document kind of it, through its control file",
        )
        p.add_argument("run", nargs="?", help="a run id or directory")
        p.add_argument(
            "--runs", type=Path, default=scratch.DEFAULT_RUNS, help=runs_help
        )
        slice_ = p.add_mutually_exclusive_group()
        slice_.add_argument("--stage", help="only this stage")
        slice_.add_argument("--kind", help="only this document kind")
        p.add_argument("--reason", default="", help="why, for the log")
        p.set_defaults(func=cmd_scratch_control)


def _scratch_plan_options(p, effort_choices, runs_help: str) -> None:
    """The options of `scratch run`, which `scratch estimate` takes too,
    so that it plans the same run."""
    from yizkr.pipeline import scratch

    p.add_argument(
        "--corpus",
        type=Path,
        help="the corpus folder (default $YIZKR_CORPUS_DIR). Refuses to "
        "run unless $YIZKR_EXCLUDE_FILE names a never-ingest list",
    )
    p.add_argument(
        "--runs", type=Path, default=scratch.DEFAULT_RUNS, help=runs_help
    )
    which = p.add_mutually_exclusive_group()
    which.add_argument(
        "--new",
        action="store_true",
        help="start a new run, with fresh state directories",
    )
    which.add_argument(
        "--resume",
        nargs="?",
        const=scratch.LATEST,
        metavar="RUN",
        help="invoke this run again (a run id, or a run directory, which "
        "must be in --runs; the latest when none is named), even one "
        "taken to the end: to go past the barrier to a corpus stage "
        "registered since, say",
    )
    p.add_argument(
        "--until",
        metavar="STAGE",
        choices=[*scratch.PHASES, *(s.name for s in scratch.CORPUS_STAGES)],
        help="stop after this stage: "
        + ", ".join(
            [*scratch.PHASES, *(s.name for s in scratch.CORPUS_STAGES)]
        )
        + " (gedcom stops with extraction)",
    )
    p.add_argument(
        "--refresh",
        action="append",
        choices=MODEL_ROLES,
        metavar="ROLE",
        help="make this role's views again once in the run, asking the "
        "engine afresh for each call even where the cache holds an answer; "
        "a later invocation with the same refresh reuses those already made "
        f"(repeatable; one of {', '.join(MODEL_ROLES)})",
    )
    p.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_CACHE,
        metavar="DIR",
        help="the LLM cache every model call goes through (default "
        "var/llm-cache; ADR-0032)",
    )
    p.add_argument("--language", default="auto", help="as for `ingest run`")
    p.add_argument(
        "--fallback-language", default="en", help="as for `ingest run`"
    )
    p.add_argument(
        "--chunk-concurrency",
        type=int,
        default=3,
        metavar="N",
        help="extraction chunks in parallel per document (default 3)",
    )
    p.add_argument(
        "--extract-effort",
        choices=effort_choices,
        default="low",
        help="reasoning effort for transcription and extraction (default low)",
    )
    p.add_argument(
        "--review-effort",
        choices=effort_choices,
        default="high",
        help="reasoning effort for the critique (default high)",
    )
    p.add_argument(
        "--extract-model",
        default="claude-sonnet-5",
        metavar="MODEL",
        help="model for extraction (default claude-sonnet-5; 'inherit' for "
        "$YIZKR_LLM_MODEL, which every other role runs); a full model id",
    )
    p.add_argument(
        "--min-free",
        type=_gibibytes,
        default=scratch.MIN_FREE_BYTES / 1024**3,
        metavar="GIB",
        help="the free space, in GiB, the run directory's and the cache's "
        "filesystems must have (default 2)",
    )


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
