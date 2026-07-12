"""User-facing errors with stable exit codes (never a raw traceback by default)."""

from __future__ import annotations

EXIT_UNEXPECTED = 1
EXIT_CONFIG = 2
EXIT_UNREACHABLE = 3
EXIT_OUTPUT = 4


class PaperlessExportError(Exception):
    """Base for all errors that should surface as a one-line human message."""

    exit_code: int = EXIT_UNEXPECTED


class ConfigError(PaperlessExportError):
    """Invalid flags, missing paths, malformed URL."""

    exit_code = EXIT_CONFIG


class AuthError(PaperlessExportError):
    """Paperless token rejected (401/403)."""

    exit_code = EXIT_CONFIG


class ServerUnreachableError(PaperlessExportError):
    """Paperless API or container not reachable."""

    exit_code = EXIT_UNREACHABLE


class OutputError(PaperlessExportError):
    """Export directory unwritable or missing."""

    exit_code = EXIT_OUTPUT


class ExporterFailedError(PaperlessExportError):
    """document_exporter exited non-zero; carries its exit code and stderr."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code if exit_code != 0 else EXIT_UNEXPECTED
