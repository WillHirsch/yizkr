"""Pipeline CLI: every step reads and writes plain files, so runs are
resumable, inspectable, and reviewable between steps.

    python -m ingest register --file scan.png --type certificate \
        --title "Marriage certificate, 1933" --pages 2
    python -m ingest transcribe --artifact Q23 --file scan.png \
        --engine textfile --text-file scan.txt --out transcription.json
    python -m ingest extract --transcription transcription.json \
        --artifact Q23 --engine file --claims-file claims.json --out batch.json
    python -m ingest resolve --batch batch.json --out resolved.json
    python -m ingest export-review --batch resolved.json --out review.csv
    python -m ingest apply-review --batch resolved.json --csv review.csv \
        --out approved.json
    python -m ingest write --batch approved.json [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ingest.claims import ClaimBatch
from ingest.transcribe import Transcription
from ingest.wiki import DEFAULT_ENV_FILE, WikiClient, WikiConfig


def _client(args) -> WikiClient:
    return WikiClient(WikiConfig.load(args.env_file))


def _load_batch(path: Path) -> ClaimBatch:
    return ClaimBatch.model_validate_json(path.read_text())


def cmd_register(args) -> int:
    from ingest.registrar import register_artifact

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
        from ingest.transcribe import TextFileTranscriber

        transcriber = TextFileTranscriber(args.text_file, language=args.language)
    elif args.engine == "llm":
        from ingest.llm import make_client
        from ingest.transcribe import LlmVisionTranscriber

        transcriber = LlmVisionTranscriber(
            make_client(args.llm), language=args.language
        )
    else:  # htr
        from ingest.transcribe import HtrPlatformAdapter

        transcriber = HtrPlatformAdapter()
    transcription = transcriber.transcribe(args.file, artifact_qid=args.artifact)
    args.out.write_text(transcription.model_dump_json(indent=2))
    print(f"wrote {args.out} ({len(transcription.text)} chars, "
          f"engine {transcription.engine})")
    return 0


def cmd_extract(args) -> int:
    transcription = Transcription.model_validate_json(
        args.transcription.read_text()
    )
    if args.engine == "file":
        if not args.claims_file:
            sys.exit("--engine file requires --claims-file")
        from ingest.extract import ClaimFileExtractor

        extractor = ClaimFileExtractor(args.claims_file)
    else:
        from ingest.extract import LlmClaimExtractor
        from ingest.llm import make_client

        extractor = LlmClaimExtractor(make_client(args.llm))
    batch = extractor.extract(transcription, artifact_qid=args.artifact)
    args.out.write_text(batch.model_dump_json(indent=2))
    print(f"wrote {args.out} ({len(batch.claims)} claims)")
    return 0


def cmd_resolve(args) -> int:
    from ingest.resolve import resolve_batch

    _, item_labels = _client(args).label_index()
    resolved = resolve_batch(_load_batch(args.batch), item_labels)
    args.out.write_text(resolved.model_dump_json(indent=2))
    print(f"wrote {args.out}")
    return 0


def cmd_export_review(args) -> int:
    from ingest.review import export_review_csv

    rows = export_review_csv(_load_batch(args.batch), args.out)
    print(f"wrote {args.out} ({rows} rows). Fill the 'decision' column "
          f"with approve/reject, then run apply-review.")
    return 0


def cmd_apply_review(args) -> int:
    from ingest.review import apply_review

    approved, warnings = apply_review(_load_batch(args.batch), args.csv)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    args.out.write_text(approved.model_dump_json(indent=2))
    print(f"wrote {args.out} ({len(approved.claims)} approved claims)")
    return 0


def cmd_write(args) -> int:
    from ingest.writer import BatchWriter

    result = BatchWriter(_client(args), dry_run=args.dry_run).write_batch(
        _load_batch(args.batch)
    )
    print(json.dumps(result.__dict__, indent=2))
    if args.dry_run:
        print("(dry run: nothing written)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest",
                                     description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register", help="upload media + artifact/fragment items")
    p.add_argument("--file", type=Path, help="media file (omit for none)")
    p.add_argument("--type", required=True,
                   help="manifest item key, e.g. photograph, certificate")
    p.add_argument("--title", required=True)
    p.add_argument("--description")
    p.add_argument("--pages", type=int, default=0,
                   help="create N page-fragment items")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("transcribe", help="media -> transcription JSON")
    p.add_argument("--file", type=Path, required=True)
    p.add_argument("--artifact", help="artifact QID")
    p.add_argument("--engine", choices=["llm", "textfile", "htr"],
                   default="textfile")
    p.add_argument("--text-file", type=Path,
                   help="existing transcription (textfile engine)")
    p.add_argument("--language", default="en")
    p.add_argument("--llm", default="anthropic",
                   help="LLM provider for --engine llm (explicit opt-in; "
                        "sends artifact content to that provider)")
    p.add_argument("--out", type=Path, default=Path("transcription.json"))
    p.set_defaults(func=cmd_transcribe)

    p = sub.add_parser("extract", help="transcription -> claim batch JSON")
    p.add_argument("--transcription", type=Path, required=True)
    p.add_argument("--artifact", required=True, help="artifact QID")
    p.add_argument("--engine", choices=["llm", "file"], default="file")
    p.add_argument("--claims-file", type=Path,
                   help="prepared claims JSON (file engine)")
    p.add_argument("--llm", default="anthropic")
    p.add_argument("--out", type=Path, default=Path("batch.json"))
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("resolve",
                       help="match local refs to existing items (exact label)")
    p.add_argument("--batch", type=Path, required=True)
    p.add_argument("--out", type=Path, default=Path("resolved.json"))
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("export-review", help="batch -> CSV for OpenRefine")
    p.add_argument("--batch", type=Path, required=True)
    p.add_argument("--out", type=Path, default=Path("review.csv"))
    p.set_defaults(func=cmd_export_review)

    p = sub.add_parser("apply-review",
                       help="filter batch by reviewed CSV decisions")
    p.add_argument("--batch", type=Path, required=True)
    p.add_argument("--csv", type=Path, required=True)
    p.add_argument("--out", type=Path, default=Path("approved.json"))
    p.set_defaults(func=cmd_apply_review)

    p = sub.add_parser("write", help="write approved claims as statements")
    p.add_argument("--batch", type=Path, required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_write)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
