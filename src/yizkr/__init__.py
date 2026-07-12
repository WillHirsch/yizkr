"""yizkr: tooling for the family-archive knowledge system.

- `yizkr.model`    — vocabulary manifest + idempotent bootstrap
- `yizkr.claims`   — the claim JSON contract between extraction and writing
- `yizkr.pipeline` — ingestion steps (register/transcribe/extract/resolve/
                     review/write) and face proposals
- `yizkr.queries`  — SPARQL query library + wiki publisher
- `yizkr.core`     — shared configuration and Wikibase client
- `yizkr.smoke`    — deployment smoke test

CLI: `yizkr --help` (see also the Makefile at the repo root).
"""

__version__ = "0.1.0"
