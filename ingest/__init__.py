"""Ingestion pipeline package.

Modules (one per pluggable step, exchanging plain files):

- `claims`     — the claim JSON contract (W1 task 3)
- `wiki`       — shared instance client and vocabulary resolution
- `registrar`  — media upload + artifact/fragment items
- `transcribe` — media -> text (LLM-vision / text file / HTR stub)
- `extract`    — transcription -> validated ClaimBatch
- `resolve`    — exact-label entity resolution (W1 minimal)
- `review`     — ClaimBatch <-> review CSV (OpenRefine)
- `writer`     — approved claims -> referenced statements (ADR-0003)

CLI: `python -m ingest --help`.
"""

__version__ = "0.1.0"
