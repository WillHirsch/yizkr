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
    yizkr scratch recheck  check a scratch run's stored outputs again with
                           the anomaly thresholds as they are now
    yizkr scratch diff     what changed between two scratch runs, and why
    yizkr scratch reset    reset the instance and repopulate it for a
                           scratch run; refuses on a kept instance

Connection settings come from deploy/.env (or $YIZKR_ENV_FILE /
--env-file); see deploy/README.md.

Exit status, one scheme for every command (yizkr.core.exits):
  0    finished, and nothing failed
  1    something failed: a document of a run, or the command itself on an
       error it does not anticipate, with a traceback
  2    stopped short; run it again once the reason it printed is dealt
       with: a paused engine or cache, a run's control file (`scratch
       run`; for `ingest run`, a stage it held), or, for `ingest run`, a
       document awaiting a review gate
  3    refused, before doing what was asked, with the reason alone: a
       usage error, an input it will not act on, or a run directory that
       records other settings
  130  interrupted (Ctrl-C)

The scratch commands give every refusal but a usage error 1, as they
do a failure; `scratch estimate` and `scratch import-transcriptions` give
a pause 1 too.
"""

from __future__ import annotations

import argparse
import dataclasses
import itertools
import json
import math
import os
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


def _placed(args) -> dict[str, frozenset[str]]:
    """The artifacts each source places, as `--placed` gives them
    (yizkr.claims.read_placed): what a caption may take as its subject
    (ADR-0039 §2.1). Exits, before any batch is read, on a value that
    gives none."""
    from yizkr.claims import read_placed

    try:
        return read_placed(args.placed)
    except ValueError as exc:
        sys.exit(f"--placed: {exc}")


def _add_placed(parser) -> None:
    parser.add_argument(
        "--placed",
        action="append",
        default=[],
        metavar="RECORD|SOURCE=SHA1,...",
        help="the artifacts a source places, which its caption and "
        "document-subject claims may take as their subject (ADR-0039 "
        "§2.1, §10.1): the record of a "
        "container's derived media the pipeline keeps in its document "
        "directory (derived-media.json, or that directory), or a source's "
        "SHA-1, '=', and the SHA-1s of what it places, comma-separated; "
        "repeatable. Without it every claim about an artifact a source "
        "places is refused",
    )


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
    list (cmd_run).

    A glob also names the bytes of an image it names inside one of the
    corpus's decks, as in a run: with the corpus folder set
    ($YIZKR_CORPUS_DIR), its decks are opened in memory to hash the
    members the globs name (corpus.follow_decks), and a deck or a folder
    that cannot be read refuses the step. With no corpus folder set, the
    globs cannot be followed into decks, and the step says so on stderr
    before it goes on."""
    from yizkr.core.corpus import (
        CORPUS_DIR_VAR,
        corpus_dir,
        follow_decks,
        is_excluded,
        load_exclusions,
    )

    if path is None and sha1 is None:
        return
    exclusions = load_exclusions(required=False, env_file=args.env_file)
    if exclusions is None:
        return
    folder = corpus_dir(args.env_file)
    if folder is not None:
        exclusions = follow_decks(exclusions, folder)
    if is_excluded(exclusions, path, sha1=sha1):
        what = path.name if path is not None else f"SHA-1 {sha1[:12]}"
        sys.exit(f"{what} is on the never-ingest list; refusing")
    if path is None and exclusions.patterns:
        sys.exit(
            "the never-ingest list names files by pattern, and no file was "
            "given to match them against: give the transcribed media file "
            "(--media); refusing"
        )
    if exclusions.patterns and folder is None:
        print(
            f"note: {CORPUS_DIR_VAR} is not set, so the never-ingest list's "
            "globs are not followed into the corpus's decks: a file whose "
            "bytes a glob names only inside a deck is not refused",
            file=sys.stderr,
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
    if not args.dry_run:
        from yizkr.core.instance import clear_contents

        # the scratch write stage does not make this write, so no scratch
        # run may take the instance for one it wrote (yizkr.core.instance)
        try:
            clear_contents(config, "yizkr bootstrap")
        except (OSError, ValueError) as exc:
            sys.exit(
                f"the recorded instance-contents key cannot be cleared "
                f"({exc}), so the bootstrap writes nothing"
            )
    bootstrap_instance(WikiClient(config), manifest, dry_run=args.dry_run)
    return 0


def cmd_smoke_test(args) -> int:
    from yizkr.smoke import run_smoke

    run_smoke(Config.load(args.env_file), timeout=args.timeout)
    return 0


class _SkipNonprivilegedExport(argparse.Action):
    """``--no-nonprivileged-export``, and its former spelling
    ``--no-public-export``, which works until the keeper run and says so
    on stderr in one line."""

    OLD = "--no-public-export"

    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(
            option_strings, dest, nargs=0, default=False, **kwargs
        )

    def __call__(self, parser, namespace, values, option_string=None):
        if option_string == self.OLD:
            print(
                f"yizkr backup: {self.OLD} is renamed "
                "--no-nonprivileged-export; the old spelling goes at the "
                "keeper run",
                file=sys.stderr,
            )
        setattr(namespace, self.dest, True)


def cmd_backup(args) -> int:
    from yizkr.backup import NonprivilegedExportRefused, run_backup

    try:
        manifest = run_backup(
            Config.load(args.env_file),
            backup_root=args.out,
            deploy_dir=args.deploy_dir,
            compose_files=args.compose_file or None,
            skip_media=args.skip_media,
            nonprivileged_export=not args.no_nonprivileged_export,
            cutoff_years=args.cutoff_years,
            keep=args.keep,
        )
    except NonprivilegedExportRefused as refusal:
        # any refusal (the query service lags, the graph changed, the dump
        # holds what the export is not made to filter): the dumps it kept,
        # then the refusal on one line for cron's log, and a non-zero exit
        if refusal.manifest is not None:
            _print_backup(args, refusal.manifest)
        print(f"non-privileged export refused: {refusal}", file=sys.stderr)
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
        adjudicate,
        apply_answers,
        attach_chains,
        build_worklist,
        export_places_csv,
        fetch_places,
        record_links,
    )

    config = Config.load(args.env_file)
    client = WikiClient(config)
    links = [
        link for run_dir in args.run or () for link in record_links(run_dir)
    ]
    if args.apply:
        if not links:
            print(
                "note: no --run directory states a link, so no row decides "
                "a question: give the --run the CSV was made with",
                file=sys.stderr,
            )
        summary = apply_answers(
            client, config, args.apply, links, "curator-reviewed"
        )
        for note in summary["notes"]:
            print(f"note: {note}", file=sys.stderr)
        print(
            f"links written: {summary['written']}, referenced on an equal "
            f"statement: {summary['referenced']}, already present: "
            f"{summary['skipped']}; refused as cycles: {summary['cycles']}, "
            f"unresolved: {summary['unresolved']}; approved rows passed "
            f"over, deciding no question asked: {summary['passed_over']}"
            + (
                f"; run item {summary['run_qid']}"
                if summary["run_qid"]
                else ""
            )
        )
        return 0

    from yizkr.pipeline.llm import make_client

    places = fetch_places(client, config)
    attach_chains(places, links)
    work = build_worklist(places, links)
    print(
        f"places: {len(places)}; links their records state: {len(links)}, "
        f"{sum(not link.claimed for link in links)} of them by a record "
        f"alone; left out, with no place to name: {work.unnamed}; "
        f"questions: {len(work.questions)}"
    )
    if not work.questions:
        print("nothing to ask: no stated link needs its container named")
        return 0
    found = adjudicate(make_client(args.llm), work.questions, places)
    rows = export_places_csv(found, places, args.out)
    if found.conflicts or found.rejected:
        print(
            f"left out: {len(found.conflicts)} question(s) answered twice "
            f"differently, {found.rejected} answer line(s) outside the "
            "answer's schema"
        )
    print(
        f"wrote {args.out} ({rows} answers). Fill the 'decision' column, "
        "then re-run with the same --run and --apply. An approved 'same' "
        "row writes the links its record states, part of the place it "
        "names, stated in the record's document; 'uncertain' rows write "
        "nothing."
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

    from yizkr.claims import (
        unplaced_claims,
        unplaced_hint,
        validate_against_manifest,
    )

    if args.json_schema:
        print(json.dumps(ClaimBatch.model_json_schema(), indent=2))
        return 0
    placed = _placed(args)
    try:
        batch = ClaimBatch.model_validate_json(args.validate.read_text())
    except ValidationError as exc:
        print(f"Schema errors in {args.validate}:\n{exc}", file=sys.stderr)
        return 1
    problems = validate_against_manifest(batch, placed=placed)
    for problem in problems:
        print(f"manifest check: {problem}", file=sys.stderr)
    unplaced = unplaced_claims(batch, placed)
    if unplaced:
        print(unplaced_hint(unplaced), file=sys.stderr)
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
    print(json.dumps(dataclasses.asdict(result), indent=2))
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


def _move_aside_earlier_batch(out: Path) -> str:
    """Move a batch an earlier run left at `out` to the first free
    `<out>.stale`, `<out>.stale.1`, ..., never over another; the clause
    that says what became of it, or "" when there was none."""
    for n in itertools.count():
        stale = out.with_name(out.name + ".stale" + (f".{n}" if n else ""))
        try:
            # claimed with O_EXCL, so no earlier stale batch is replaced
            os.close(os.open(stale, os.O_WRONLY | os.O_CREAT | os.O_EXCL))
        except FileExistsError:
            continue
        except OSError as exc:
            failed = exc
            break
        try:
            out.replace(stale)
        except FileNotFoundError:
            stale.unlink(missing_ok=True)
            return ""
        except OSError as exc:
            stale.unlink(missing_ok=True)
            failed = exc
            break
        return f"; the earlier batch there moved to {stale}"
    if not os.path.lexists(out):
        return ""
    return (
        f"; an earlier batch is still at {out} and could not be moved "
        f"aside ({failed.strerror or failed}): it is not this run's"
    )


def cmd_extract(args) -> int:
    from pydantic import ValidationError

    from yizkr.claims import ArtifactRef
    from yizkr.core.exits import EXIT_FAILED
    from yizkr.pipeline.transcribe import Transcription

    # an input that cannot be read is refused in one line, before any
    # engine is reached
    try:
        text = args.transcription.read_text(encoding="utf-8")
    except OSError as exc:
        sys.exit(
            f"cannot read the transcription {args.transcription}: "
            f"{exc.strerror or exc}; refusing"
        )
    except UnicodeDecodeError:
        sys.exit(
            f"{args.transcription.name} is not UTF-8 text, so not a "
            f"transcription this step reads; refusing"
        )
    try:
        transcription = Transcription.model_validate_json(text)
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
    # and for the media file by name and hash when one is given. A name
    # the list's patterns match is refused before the file is read, so a
    # listed file is refused as listed even when it cannot be read.
    try:
        _refuse_if_excluded(args, args.media, sha1=transcription.artifact.sha1)
        media = (
            None if args.media is None else ArtifactRef.from_path(args.media)
        )
    except OSError as exc:
        # only the media's own read is the media's fault: the list's env
        # file, say, keeps its own error
        if exc.filename is None or Path(exc.filename) != args.media:
            raise
        sys.exit(
            f"cannot read the media {args.media}: {exc.strerror or exc}; "
            f"refusing"
        )
    if media is not None and media != transcription.artifact:
        sys.exit(
            f"{args.media.name} is not the media {args.transcription.name} "
            f"transcribes (its SHA-1 differs); refusing"
        )
    if args.out.is_dir():
        sys.exit(f"--out {args.out} is a folder, not a batch file; refusing")
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
    from yizkr.pipeline.extract import ExtractionFailed

    try:
        batch = extractor.extract(transcription, transcription.artifact)
    except BaseException as exc:
        # whatever ends the extraction (a failure, a pause, Ctrl-C, a bug),
        # an earlier run's batch at --out is moved aside to a free
        # <out>.stale name, so no later step reads it as this run's. Moved,
        # not deleted: it may be the one copy of an earlier extraction.
        moved = _move_aside_earlier_batch(args.out)
        if not isinstance(exc, ExtractionFailed):
            # the exception keeps its own handling, after one line on what
            # became of the earlier batch
            if moved:
                print(f"{args.out} not written{moved}", file=sys.stderr)
            raise
        # a failure, as a partial batch is, not a refusal: an engine's
        # extraction may succeed on another run, from the LLM cache at no
        # engine cost for the calls that completed, and a claims file once
        # it is fixed. No batch is written, so none reads as a claim-free
        # document's.
        print(
            f"extraction failed: {' '.join(str(exc).split())}; "
            f"{args.out} not written{moved}",
            file=sys.stderr,
        )
        return EXIT_FAILED
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(batch.model_dump_json(indent=2))
    print(f"wrote {args.out} ({len(batch.claims)} claims)")
    if not batch.claims:
        # why a batch without claims is empty, as the extractor accepted it
        for reason in getattr(extractor, "attention", None) or ():
            print(f"attention: {reason}", file=sys.stderr)
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
            f"warning: {len(failed)} chunks failed "
            f"({', '.join(failed)}); batch is PARTIAL — re-run to retry "
            f"them (with --cache DIR, the calls that completed are "
            f"answered from the cache)",
            file=sys.stderr,
        )
        return EXIT_FAILED
    return 0


def _identity_map(args):
    """The identity map `--identity-map` names, or None."""
    if args.identity_map is None:
        return None
    from yizkr.pipeline.clustering import ClusteringInputError, IdentityMap

    try:
        return IdentityMap.read(args.identity_map)
    except ClusteringInputError as exc:
        sys.exit(str(exc))


def cmd_resolve(args) -> int:
    from yizkr.pipeline.clustering import UnmappedMention
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
    try:
        resolved = resolve_batch(batch, _identity_map(args))
    except UnmappedMention as exc:
        sys.exit(str(exc))
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
    if result.outcomes:
        print(
            "GEDCOM outcomes (counted): "
            + format_unsupported_tags(result.outcomes),
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
    candidates = block_candidates(batch.entities, roster, batch=batch)
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
        fetch_place_containment,
        fetch_roster,
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

    try:
        approved, warnings = apply_review(_load_batch(args.batch), args.csv)
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))
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
            f"`ingest run` writes them as low-scrutiny for reappraisal "
            f"while no row has another decision)"
        )
    return 0


def cmd_write(args) -> int:
    from pydantic import ValidationError

    from yizkr.claims import unplaced_claims, unplaced_hint
    from yizkr.pipeline.clustering import UnmappedMention
    from yizkr.pipeline.review import unapproved_depicts
    from yizkr.pipeline.writer import (
        BatchWriter,
        OtherIdentityMap,
        UnapprovedDepictsError,
        UnregisteredArtifactError,
        mapped_clusters,
    )

    # a batch file that does not load, a --placed that gives no placed
    # media, or a local entity the identity map does not hold, is refused
    # before the instance is contacted; an unapproved depicts claim is
    # refused first, by the writer
    placed = _placed(args)
    try:
        batch = _load_batch(args.batch)
    except OSError as exc:
        sys.exit(f"{args.batch}: {exc.strerror}")
    except ValidationError as exc:
        found = "; ".join(
            f"{'.'.join(map(str, error['loc'])) or 'file'}: {error['msg']}"
            for error in exc.errors()[:3]
        )
        sys.exit(
            f"{args.batch.name} is not a claim batch this step writes "
            f"({found}); nothing was written"
        )
    identity = _identity_map(args)
    if not unapproved_depicts(batch):
        try:
            mapped_clusters(batch, identity)
        except UnmappedMention as exc:
            sys.exit(str(exc))
    # the writer's refusals, each before its first write
    try:
        result = BatchWriter(_client(args), dry_run=args.dry_run).write_batch(
            batch, identity=identity, placed=placed
        )
    except (
        UnapprovedDepictsError,
        UnregisteredArtifactError,
        OtherIdentityMap,
    ) as exc:
        sys.exit(str(exc))
    print(json.dumps(dataclasses.asdict(result), indent=2))
    unplaced = unplaced_claims(batch, placed)
    if unplaced:
        print(unplaced_hint(unplaced), file=sys.stderr)
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


def cmd_gaps_rollup(args) -> int:
    import csv

    from yizkr.pipeline import gaps

    run = _scratch_run_dir(args)
    try:
        written, result = gaps.write_rollup(run, args.out)
    except (OSError, ValueError, csv.Error) as exc:
        # a file that cannot be read or written, or a gap file that is no
        # UTF-8 text or no CSV
        sys.exit(str(exc))
    print(
        f"{result.gaps} gap(s) under {len(result.rows)} predicate(s), from "
        f"{result.documents_with_gaps} of {result.documents_read} "
        f"document(s): {written}"
    )
    print(
        f"{written.name} quotes the documents' evidence, and stays local; "
        f"its decision column is the curator's"
    )
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


def cmd_scratch_recheck(args) -> int:
    from yizkr.pipeline import anomalies

    run = _scratch_run_dir(args)
    try:
        config = anomalies.load_config(args.config)
    except anomalies.AnomalyConfigError as exc:
        sys.exit(str(exc))
    try:
        alerts = anomalies.recheck(run, config)
    except OSError as exc:
        sys.exit(str(exc))
    for line in anomalies.recheck_lines(run, config, alerts):
        print(line)
    return 0


def cmd_scratch_diff(args) -> int:
    from yizkr.pipeline import rundiff, scratch

    named = args.run
    if len(named) > 2:
        sys.exit("name two runs at most: the baseline, then the run")
    try:
        run = scratch.resolve_run(args.runs, named[-1] if named else None)
        if len(named) == 2:
            baseline = scratch.resolve_run(args.runs, named[0])
        else:
            baseline = rundiff.baseline_run(
                args.runs, run, refs=args.main or rundiff.MAIN_REFS
            )
    except (scratch.ScratchRefused, rundiff.DiffError) as exc:
        sys.exit(str(exc))
    if baseline.resolve() == run.resolve():
        sys.exit(f"{run.name} is the baseline itself: name another run")
    try:
        written = rundiff.write_diff(
            baseline, run, cache=args.cache, rebuild=args.rebuild
        )
    except rundiff.DiffError as exc:
        sys.exit(str(exc))
    print(f"baseline   {baseline}")
    print(f"run        {run}")
    for name in (rundiff.MARKDOWN_FILE, rundiff.JSON_FILE):
        print(f"{name:10} {written[name]}")
    print(
        f"{rundiff.MARKDOWN_FILE} holds the aggregates alone, and may be "
        f"posted; {rundiff.JSON_FILE} stays local (docs/RUN_DIFF.md)"
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


def cmd_scratch_reset(args) -> int:
    from yizkr import reset
    from yizkr.pipeline.scratch import (
        EXIT_FAILED,
        EXIT_INTERRUPTED,
        EXIT_OK,
    )

    try:
        report = reset.reset_instance(
            env_file=args.env_file,
            deploy_dir=args.deploy_dir,
            compose_files=args.compose_file,
            yes=args.yes,
            start_timeout=args.start_timeout,
            smoke_timeout=args.smoke_timeout,
        )
    except reset.ResetRefused as exc:
        print(f"the reset is refused: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except reset.ResetFailed as exc:
        print(f"the reset failed: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        print(
            "interrupted: every step is safe to repeat, so `yizkr scratch "
            "reset` again finishes the reset",
            file=sys.stderr,
        )
        return EXIT_INTERRUPTED
    for line in reset.describe(report):
        print(line)
    return EXIT_OK


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
    parser = argparse.ArgumentParser(
        prog="yizkr",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
        "non-privileged export (ADR-0020)",
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
        "--no-nonprivileged-export",
        "--no-public-export",
        dest="no_nonprivileged_export",
        action=_SkipNonprivilegedExport,
        help="skip the possibly-living-excluded export (--no-public-export: "
        "alias until the keeper run)",
    )
    p.add_argument(
        "--cutoff-years",
        type=int,
        default=DEFAULT_CUTOFF_YEARS,
        metavar="N",
        help="without any death evidence, only people born more than N "
        "years ago enter the non-privileged export "
        f"(default {DEFAULT_CUTOFF_YEARS})",
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
        help="ask which existing place each container the places' mention "
        "records state means (ADR-0009, ADR-0038); approved answers write "
        "the links the sources state, as referenced part_of statements",
    )
    p.add_argument(
        "--llm", default="anthropic", choices=["anthropic", "claude-code"]
    )
    p.add_argument("--out", type=Path, default=Path("place-hierarchy.csv"))
    p.add_argument(
        "--run",
        type=Path,
        action="append",
        help="a run directory that wrote the graph: the chains of "
        "containers its places' mention records state are the links asked "
        "of (repeatable; needed by --apply too)",
    )
    p.add_argument(
        "--apply",
        type=Path,
        help="reviewed CSV: write the links its approved rows decide",
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
    _add_placed(p)
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
    p.add_argument(
        "--out",
        type=Path,
        default=Path("batch.json"),
        help="the claim batch file; when the extraction does not complete, "
        "an earlier batch here is moved to the first free <out>.stale "
        "(or <out>.stale.N). Beside it go <out>.chunks/ (chunked) or "
        "<out>.raw.txt and <out>.rejected.json (single call)",
    )
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
        "resolve",
        help="apply approved equivalences; every other local ref stays "
        "local, for the write to resolve through the identity map",
    )
    p.add_argument("--batch", type=Path, required=True)
    p.add_argument(
        "--equivalences",
        type=Path,
        help="reviewed equivalences CSV from `ingest reconcile`; approved "
        "rows resolve to existing items",
    )
    p.add_argument(
        "--identity-map",
        type=Path,
        help="the identity map the batch is to be written with "
        "(identity-map.json); refuses a batch with a mention it lacks",
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
            "curator bulk-accept of a review CSV: write bulk-accept on every "
            "claim (review.csv), which `ingest run` writes under a distinct "
            "low-scrutiny edit summary while no row has another decision "
            "(ADR-0023), or ratify each machine verdict (equivalences.csv); "
            "refuses a whole claims sheet with "
            "any depicts row (ADR-0005), or without an exact claim_id and "
            "exactly one property column"
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
    p.add_argument(
        "--identity-map",
        type=Path,
        help="the identity map (identity-map.json) every local entity of "
        "the batch finds or mints its item through; required when the "
        "batch names one, as a single batch written without a map, such "
        "as a GEDCOM export's or a face batch's, is refused (the scratch "
        "run's write is the write path)",
    )
    _add_placed(p)
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
        "before the next reconciles; each write finds the items of its "
        "entities through the run's identity map, never by label, and "
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

    from yizkr.pipeline import scratch

    gaps_group = sub.add_parser(
        "gaps",
        help="the open-schema harness's relation gaps across a run "
        "(ADR-0013 §B)",
    )
    gsub = gaps_group.add_subparsers(dest="action", required=True)
    p = gsub.add_parser(
        "rollup",
        help="every gap a run's (the latest scratch run by default) "
        "documents logged at extraction, one row per normalised "
        "predicate with its frequency and an empty decision column, "
        "written to gaps-rollup.csv in the run directory for OpenRefine "
        "(src/yizkr/pipeline/gaps.py); it quotes evidence, so it stays "
        "local",
    )
    p.add_argument("run", nargs="?", help="a run id or directory")
    p.add_argument(
        "--runs",
        type=Path,
        default=scratch.DEFAULT_RUNS,
        help=f"the runs directory (default {scratch.DEFAULT_RUNS}); a run "
        "is named by its id there, or by its directory's path",
    )
    p.add_argument(
        "--out",
        type=Path,
        metavar="FILE",
        help="write the CSV here instead (keep it out of the repository)",
    )
    p.set_defaults(func=cmd_gaps_rollup)

    _scratch_parsers(sub, effort_choices)
    return parser


def _scratch_parsers(sub, effort_choices) -> None:
    """`yizkr scratch`: the scratch driver and its call estimate
    (docs/runbooks/scratch-run.md), the import of archived transcriptions
    for its calls (docs/LLM_CACHE.md, Seeding), the run monitor's
    commands (docs/MONITOR.md), the anomaly recheck (docs/ANOMALIES.md),
    and the reset of the instance for a run."""
    from yizkr.pipeline import scratch
    from yizkr.pipeline.rundir import CONTROL_ACTIONS

    group = sub.add_parser(
        "scratch",
        help="whole-corpus scratch runs: import archived transcriptions for "
        "one's calls, estimate its calls, run it, follow it, pause, resume "
        "or abort it, report it, check its outputs again for anomalies, "
        "diff it with another, and reset the "
        "instance for it "
        "(docs/runbooks/scratch-run.md)",
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

    p = ssub.add_parser(
        "recheck",
        help="check a scratch run's (the latest by default) stored outputs "
        "again with the anomaly thresholds as they are now, without "
        "recomputing any stage, and append the alerts they raise to its "
        "alerts (docs/ANOMALIES.md, Recheck)",
    )
    p.add_argument("run", nargs="?", help="a run id or directory")
    p.add_argument(
        "--runs", type=Path, default=scratch.DEFAULT_RUNS, help=runs_help
    )
    p.add_argument(
        "--config",
        type=Path,
        metavar="FILE",
        help="a file of thresholds over the defaults, in place of the one "
        "$YIZKR_ANOMALIES names",
    )
    p.set_defaults(func=cmd_scratch_recheck)

    p = ssub.add_parser(
        "diff",
        help="what changed between two scratch runs, and why: stages "
        "reused or recomputed (by the key component that changed), "
        "figures, anomalies, failures, claims, mentions and clusters. "
        "Writes diff.json, which stays local, and diff.md, the aggregates "
        "alone, which may be posted, into the run's .diff folder "
        "(docs/RUN_DIFF.md)",
        description="Diffs a run (the latest by default) against a "
        "baseline (by default the latest completed run on main in the "
        "runs directory that is older than the run, run on clean code). "
        "Name one run to diff it with "
        "that baseline, or two: the baseline, then the run. It reads the "
        "two run directories, and of the LLM cache only the components of "
        "the keys their events name; never the wiki, the engine or the "
        "corpus.",
    )
    p.add_argument(
        "run",
        nargs="*",
        help="the run, or the baseline then the run (run ids or directories)",
    )
    p.add_argument(
        "--runs", type=Path, default=scratch.DEFAULT_RUNS, help=runs_help
    )
    p.add_argument(
        "--main",
        action="append",
        metavar="REF",
        help="the git ref that is main, for the default baseline "
        "(default origin/main, else main; repeatable, the first git "
        "knows is taken)",
    )
    p.add_argument(
        "--cache",
        type=Path,
        metavar="DIR",
        help="the LLM cache whose entries' key components attribute each "
        "recompute (default the one the run's run.json names)",
    )
    p.add_argument(
        "--rebuild",
        action="store_true",
        help="build both runs' reports afresh, rather than read the "
        "report.json each holds",
    )
    p.set_defaults(func=cmd_scratch_diff)

    _scratch_reset_parser(ssub)

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


def _scratch_reset_parser(ssub) -> None:
    """`yizkr scratch reset` (docs/runbooks/scratch-run.md, Reset the
    instance)."""
    from yizkr import reset

    p = ssub.add_parser(
        "reset",
        help="reset the instance and repopulate it for a scratch run: stop "
        "the stack, remove its graph volumes (database, search index, query "
        "service, uploaded media), start it afresh, bootstrap the manifest, "
        "publish the queries and run the smoke test, timing each step. It "
        "refuses on a kept instance, holds the live-stack lock, and takes "
        "no backup (docs/runbooks/scratch-run.md, Reset the instance)",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="confirm the reset without being asked; a reset run without a "
        "terminal needs it",
    )
    p.add_argument(
        "--deploy-dir",
        type=Path,
        help="the stack's deploy directory (default: the directory of the "
        "env file, which its compose files read)",
    )
    p.add_argument(
        "--compose-file",
        action="append",
        metavar="FILE",
        help="a compose file the stack was started with, relative to "
        "--deploy-dir (repeatable; default docker-compose.yml)",
    )
    p.add_argument(
        "--start-timeout",
        type=float,
        default=reset.DEFAULT_START_TIMEOUT,
        metavar="SECONDS",
        help="how long the stack may take to start and be healthy "
        f"(default {reset.DEFAULT_START_TIMEOUT:g})",
    )
    p.add_argument(
        "--smoke-timeout",
        type=int,
        default=reset.DEFAULT_SMOKE_TIMEOUT,
        metavar="SECONDS",
        help="how long the smoke test waits for the query service "
        f"(default {reset.DEFAULT_SMOKE_TIMEOUT})",
    )
    p.set_defaults(func=cmd_scratch_reset)


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


def main(argv: list[str] | None = None) -> int:
    """Run the command `argv` names (by default the command line's), and
    return its exit status (yizkr.core.exits). A refusal (`sys.exit` with
    its reason, which a scratch command gives a failure's status, or a run
    directory's RunSettingsConflict), a pause
    (PassPaused) and Ctrl-C print their message alone, on stderr, and
    return their own status; a usage error returns the refusal's, not
    argparse's 2, which is a pause's."""
    from yizkr.core.exits import (
        EXIT_FAILED,
        EXIT_INTERRUPTED,
        EXIT_REFUSED,
        EXIT_STOPPED,
    )
    from yizkr.pipeline.llm import PassPaused
    from yizkr.pipeline.rundir import RunSettingsConflict

    try:
        # argparse prints the usage error before it exits
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        if exc.code == 2:
            return EXIT_REFUSED
        raise
    try:
        return args.func(args)
    except SystemExit as exc:
        if not isinstance(exc.code, str):
            raise
        print(exc.code, file=sys.stderr)
        # the scratch commands give a refusal a failure's status
        return EXIT_FAILED if args.command == "scratch" else EXIT_REFUSED
    except RunSettingsConflict as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_REFUSED
    except PassPaused as exc:
        # the engine's own account of the pause, and what to do
        print(str(exc), file=sys.stderr)
        return EXIT_STOPPED
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
