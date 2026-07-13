"""yizkr — one CLI for every tool in the family-archive system.

    yizkr bootstrap        apply model/manifest.yaml to the instance
    yizkr smoke-test       verify a deployment end to end
    yizkr publish-queries  publish the SPARQL library to wiki pages
    yizkr claims           claim-contract utilities (JSON schema, validate)
    yizkr ingest <step>    pipeline steps; every step reads/writes plain
                           files so runs pause for review and resume

Connection settings come from deploy/.env (or $YIZKR_ENV_FILE /
--env-file); see deploy/README.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from yizkr.claims import ClaimBatch
from yizkr.core.config import Config
from yizkr.core.wiki import WikiClient


def _client(args) -> WikiClient:
    return WikiClient(Config.load(args.env_file))


def _load_batch(path: Path) -> ClaimBatch:
    return ClaimBatch.model_validate_json(path.read_text())


# -- top-level commands -------------------------------------------------


def cmd_bootstrap(args) -> int:
    from yizkr.model.bootstrap import bootstrap_instance
    from yizkr.model.schema import load_manifest

    manifest = load_manifest(args.manifest) if args.manifest else load_manifest()
    config = Config.load(args.env_file)
    print(f"Wikibase: {config.wikibase_url}")
    bootstrap_instance(WikiClient(config), manifest, dry_run=args.dry_run)
    return 0


def cmd_smoke_test(args) -> int:
    from yizkr.smoke import run_smoke

    run_smoke(Config.load(args.env_file), timeout=args.timeout)
    return 0


def cmd_backup(args) -> int:
    from yizkr.backup import run_backup

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
    for name, info in manifest["components"].items():
        detail = "".join(
            f", {key}={value}" for key, value in info.items() if key != "file"
        )
        print(f"{name}: {args.out / manifest['timestamp'] / info['file']}{detail}")
    if manifest["pruned"]:
        print(f"pruned old backups: {', '.join(manifest['pruned'])}")
    return 0


def cmd_merge_items(args) -> int:
    client = _client(args)
    for qid in (args.from_qid, args.to_qid):
        if not client.fetch_entities([qid]).get(qid):
            sys.exit(f"{qid} does not exist")
    client.merge_items(args.from_qid, args.to_qid, summary=args.summary)
    print(
        f"merged {args.from_qid} into {args.to_qid}; {args.from_qid} is "
        f"now a redirect"
    )
    return 0


def cmd_publish_queries(args) -> int:
    from yizkr.queries.publish import publish_queries

    config = Config.load(args.env_file)
    publish_queries(WikiClient(config), config, dry_run=args.dry_run)
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
    if args.engine == "textfile":
        if not args.text_file:
            sys.exit("--engine textfile requires --text-file")
        from yizkr.pipeline.transcribe import TextFileTranscriber

        transcriber = TextFileTranscriber(args.text_file, language=args.language)
    elif args.engine == "llm":
        from yizkr.pipeline.llm import make_client
        from yizkr.pipeline.transcribe import LlmVisionTranscriber

        transcriber = LlmVisionTranscriber(
            make_client(args.llm), language=args.language
        )
    else:  # htr
        from yizkr.pipeline.transcribe import HtrPlatformAdapter

        transcriber = HtrPlatformAdapter()
    transcription = transcriber.transcribe(args.file, artifact_qid=args.artifact)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(transcription.model_dump_json(indent=2))
    print(
        f"wrote {args.out} ({len(transcription.text)} chars, "
        f"engine {transcription.engine})"
    )
    return 0


def cmd_extract(args) -> int:
    from yizkr.pipeline.transcribe import Transcription

    transcription = Transcription.model_validate_json(args.transcription.read_text())
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
        from yizkr.pipeline.llm import make_client

        chunked = args.strategy == "chunked" or (
            args.strategy == "auto" and len(transcription.text) > AUTO_CHUNK_THRESHOLD
        )
        if chunked:
            extractor = ChunkedClaimExtractor(
                make_client(args.llm),
                state_dir=args.out.with_name(args.out.name + ".chunks"),
                budget=args.chunk_budget,
                retries=args.chunk_retries,
                resume=args.resume,
            )
        else:
            raw_dump = args.out.with_name(args.out.name + ".raw.txt")
            rejects_dump = args.out.with_name(args.out.name + ".rejected.json")
            extractor = LlmClaimExtractor(
                make_client(args.llm), raw_dump=raw_dump, rejects_dump=rejects_dump
            )
    batch = extractor.extract(transcription, artifact_qid=args.artifact)
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
            f"({', '.join(failed)}); batch is PARTIAL — re-run with "
            f"--resume to retry only those chunks",
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
        )

        approved = read_approved_equivalences(args.equivalences)
        batch = apply_equivalences(batch, approved)
        print(f"applied {len(approved)} approved equivalences")
    _, item_labels = _client(args).label_index()
    resolved = resolve_batch(batch, item_labels)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(resolved.model_dump_json(indent=2))
    print(f"wrote {args.out}")
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
    proposals = adjudicate(make_client(args.llm), batch.entities, candidates, batch)
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
        fetch_roster,
        propose_graph_pairs,
    )

    config = Config.load(args.env_file)
    roster = fetch_roster(WikiClient(config), config)
    proposals = propose_graph_pairs(make_client(args.llm), roster)
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
        f"wrote {args.out} ({rows} rows). Fill the 'decision' column "
        f"with approve/reject, then run apply-review."
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


def cmd_write(args) -> int:
    from yizkr.pipeline.writer import BatchWriter

    result = BatchWriter(_client(args), dry_run=args.dry_run).write_batch(
        _load_batch(args.batch)
    )
    print(json.dumps(result.__dict__, indent=2))
    if args.dry_run:
        print("(dry run: nothing written)")
    return 0


def cmd_detect_faces(args) -> int:
    from yizkr.pipeline.faces import Gallery, make_detector, proposals_to_batch

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


def cmd_match_faces(args) -> int:
    from yizkr.pipeline.faces import Gallery, load_detections, proposals_to_batch

    detection = load_detections(args.detections)
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
    from yizkr.pipeline.faces import (
        Gallery,
        enroll_from_detections,
        load_detections,
    )

    gallery = Gallery.load(args.gallery)
    person = enroll_from_detections(
        load_detections(args.detections),
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

    p = sub.add_parser("bootstrap", help="apply the model manifest to the instance")
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
        help="database + media + RDF dump, and a living-persons-excluded "
        "public export (W1 task 7)",
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
        help="skip the living-persons-excluded export",
    )
    p.add_argument(
        "--cutoff-years",
        type=int,
        default=100,
        metavar="N",
        help="without death evidence or a curator flag, only people born "
        "more than N years ago enter the public export (default 100)",
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
    p.add_argument("--llm", default="anthropic", choices=["anthropic", "claude-code"])
    p.add_argument("--out", type=Path, default=Path("graph-equivalences.csv"))
    p.set_defaults(func=cmd_reconcile_graph)

    p = sub.add_parser("claims", help="claim-contract utilities")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--json-schema", action="store_true", help="print the ClaimBatch JSON Schema"
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

    p = isub.add_parser("register", help="upload media + artifact/fragment items")
    p.add_argument("--file", type=Path, help="media file (omit for none)")
    p.add_argument(
        "--type", required=True, help="manifest item key, e.g. photograph, certificate"
    )
    p.add_argument("--title", required=True)
    p.add_argument("--description")
    p.add_argument("--pages", type=int, default=0, help="create N page-fragment items")
    p.set_defaults(func=cmd_register)

    p = isub.add_parser("transcribe", help="media -> transcription JSON")
    p.add_argument("--file", type=Path, required=True)
    p.add_argument("--artifact", help="artifact QID")
    p.add_argument("--engine", choices=["llm", "textfile", "htr"], default="textfile")
    p.add_argument(
        "--text-file", type=Path, help="existing transcription (textfile engine)"
    )
    p.add_argument("--language", default="en")
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
    p.add_argument("--transcription", type=Path, required=True)
    p.add_argument("--artifact", required=True, help="artifact QID")
    p.add_argument("--engine", choices=["llm", "file"], default="file")
    p.add_argument(
        "--claims-file", type=Path, help="prepared claims JSON (file engine)"
    )
    p.add_argument("--llm", default="anthropic", choices=["anthropic", "claude-code"])
    p.add_argument("--out", type=Path, default=Path("batch.json"))
    p.add_argument(
        "--strategy",
        choices=["auto", "single", "chunked"],
        default="auto",
        help="chunked extraction (ADR-0006): auto picks chunked for "
        "large transcriptions; state lives in <out>.chunks/",
    )
    p.add_argument("--chunk-budget", type=int, default=2000, metavar="CHARS")
    p.add_argument("--chunk-retries", type=int, default=2, metavar="N")
    p.add_argument(
        "--resume",
        action="store_true",
        help="reuse chunk state from a previous run; only missing/"
        "failed chunks are re-run (persisted raw responses are "
        "re-parsed before re-invoking the model)",
    )
    p.set_defaults(func=cmd_extract)

    p = isub.add_parser(
        "reconcile",
        help="propose entity equivalences against the graph (ADR-0007)",
    )
    p.add_argument("--batch", type=Path, required=True)
    p.add_argument("--llm", default="anthropic", choices=["anthropic", "claude-code"])
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

    p = isub.add_parser("apply-review", help="filter batch by reviewed CSV decisions")
    p.add_argument("--batch", type=Path, required=True)
    p.add_argument("--csv", type=Path, required=True)
    p.add_argument("--out", type=Path, default=Path("approved.json"))
    p.set_defaults(func=cmd_apply_review)

    p = isub.add_parser("write", help="write approved claims as statements")
    p.add_argument("--batch", type=Path, required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_write)

    p = isub.add_parser(
        "detect-faces", help="detect faces in a photo -> depicts proposals"
    )
    p.add_argument("--file", type=Path, required=True, help="photo file")
    p.add_argument(
        "--artifact", required=True, help="QID of the registered photograph item"
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
        "--face", type=int, required=True, help="face index within the detections file"
    )
    p.add_argument("--label", required=True, help="person's English label")
    p.add_argument("--qid", help="person item QID, if it exists")
    p.add_argument("--gallery", type=Path, default=Path("gallery.json"))
    p.set_defaults(func=cmd_enroll_face)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
