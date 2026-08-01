"""Bounded persistent logging with centralized runtime secret redaction."""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3
_secrets: set[str] = set()
_URL_PATTERN = re.compile(r"https?://[^\s)\]}>\"']+", re.IGNORECASE)
_SENSITIVE_QUERY_KEY = re.compile(
    r"(?:api[-_]?key|auth|authorization|credential|key|pass(?:phrase|word)?|secret|token)",
    re.IGNORECASE,
)


def sanitize_url(value: str) -> str:
    """Return a useful URL representation without embedded credentials."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[REDACTED URL]"
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return "[REDACTED URL]"
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        host = f"{host}:{port}"
    query = urlencode(
        [
            (key, "[REDACTED]" if _SENSITIVE_QUERY_KEY.search(key) else item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parsed.scheme, host, parsed.path, query, parsed.fragment))


def sanitize_text(value: str) -> str:
    """Redact registered secrets and credentials embedded in arbitrary text."""
    message = _URL_PATTERN.sub(lambda match: sanitize_url(match.group(0)), value)
    for secret in sorted(_secrets, key=len, reverse=True):
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = sanitize_text(record.getMessage())
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
