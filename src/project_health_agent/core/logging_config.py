"""
Centralized logging configuration.

Enterprise runs (cron/CI/containers) need structured, greppable logs rather
than ad-hoc print() calls — this module is imported once, at process start,
by every CLI entrypoint (see cli/weekly.py, cli/monthly.py).

Two output shapes are supported:
    - "text"  (default): human-readable, for local dev / terminal use
    - "json":             one JSON object per line, for log aggregation
                           (CloudWatch, Datadog, ELK, etc.) — enable with
                           LOG_FORMAT=json in the environment.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from project_health_agent.core.config import settings


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


_TEXT_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

_configured = False


def configure_logging() -> None:
    """Idempotent: safe to call from multiple entrypoints/tests."""
    global _configured
    if _configured:
        return

    root = logging.getLogger("project_health_agent")
    root.setLevel(settings.LOG_LEVEL)
    root.propagate = False

    handler = logging.StreamHandler(stream=sys.stdout)
    if settings.LOG_FORMAT == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT, datefmt="%Y-%m-%dT%H:%M:%S"))

    root.handlers = [handler]
    _configured = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(f"project_health_agent.{name}")
