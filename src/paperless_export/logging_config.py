"""Bounded persistent logging with centralized runtime secret redaction."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3
_secrets: set[str] = set()


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in sorted(_secrets, key=len, reverse=True):
            if secret:
                message = message.replace(secret, "[REDACTED]")
        record.msg = message
        record.args = ()
        return True


class _ExcludeChildTranscript(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.endswith(".transcript")


def register_secret(value: str | None) -> None:
    if value:
        _secrets.add(value)


def configure_logging(log_file: Path, *, verbose: bool) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for handler in tuple(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.DEBUG)
    redactor = _RedactingFilter()

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(
        logging.Formatter("%(levelname)s %(name)s: %(message)s" if verbose else "%(message)s")
    )
    console.addFilter(redactor)
    console.addFilter(_ExcludeChildTranscript())

    logfile = RotatingFileHandler(
        log_file,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUPS,
        encoding="utf-8",
    )
    logfile.setLevel(logging.DEBUG)
    logfile.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logfile.addFilter(redactor)
    root.addHandler(console)
    root.addHandler(logfile)
