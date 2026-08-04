"""User-facing errors with stable exit codes (never a raw traceback by default)."""

from __future__ import annotations

from .exit_codes import ExitCode


class PaperlessExportError(Exception):
    """Base for all errors that should surface as a one-line human message."""

    exit_code: int = ExitCode.FATAL


class ConfigError(PaperlessExportError):
    """Invalid flags, missing paths, malformed URL."""

    exit_code = ExitCode.USAGE


class AuthError(PaperlessExportError):
    """Paperless token rejected (401/403)."""

    exit_code = ExitCode.USAGE


class ServerUnreachableError(PaperlessExportError):
    """Paperless API or container not reachable."""

    exit_code = ExitCode.CONFLICT


class OutputError(PaperlessExportError):
    """Export directory unwritable or missing."""

    exit_code = ExitCode.FATAL


class UnsafeOutputError(OutputError):
    """Manifest-derived or generated output escaped the export boundary."""


class PartialOutputError(PaperlessExportError):
    """The exporter succeeded, but requested post-processing was incomplete."""

    exit_code = ExitCode.PARTIAL


class ExporterFailedError(PaperlessExportError):
    """document_exporter ran but failed; carries its original child code."""

    exit_code = ExitCode.FATAL

    def __init__(self, message: str, child_code: int) -> None:
        super().__init__(message)
        self.child_code = child_code
