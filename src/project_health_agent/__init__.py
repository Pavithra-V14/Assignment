"""Project Health Reporting Agent.

Enterprise package layout:
    core/         settings, logging, exceptions
    ingestion/    data source adapters (local filesystem, Google Drive) + cleaning
    scoring/      deterministic RAG scoring engine (Phase 1 methodology)
    reasoning/    LLM reasoning layer + provider abstraction (Phase 2)
    reporting/    document rendering (weekly .docx)
    aggregation/  portfolio aggregation + slide synthesis + deck rendering (Phase 3)
    cli/          Typer entrypoints (console scripts)
"""
__version__ = "2.0.0"
