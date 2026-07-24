"""User-facing errors with stable exit codes (never a raw traceback by default)."""

from __future__ import annotations

EXIT_UNEXPECTED = 1
EXIT_CONFIG = 2
EXIT_UNREACHABLE = 3
EXIT_OUTPUT = 4
EXIT_PARTIAL = 5
EXIT_EXPORTER_FAILED = 6


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


class UnsafeOutputError(OutputError):
    """Manifest-derived or generated output escaped the export boundary."""


class PartialOutputError(PaperlessExportError):
    """The exporter succeeded, but requested post-processing was incomplete."""

    exit_code = EXIT_PARTIAL


class ExporterFailedError(PaperlessExportError):
    """document_exporter ran but failed; carries its original child code."""

    exit_code = EXIT_EXPORTER_FAILED

    def __init__(self, message: str, child_code: int) -> None:
        super().__init__(message)
        self.child_code = child_code
