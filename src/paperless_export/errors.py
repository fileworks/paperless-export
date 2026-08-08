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
    """Writing the export failed after work had already begun.

    The line between this and `OutputPathError` is the one the exit-code table
    already draws: *was anything attempted?* A write that fails part-way leaves
    output on disk in a state neither the tool nor the operator can characterise,
    which is what `FATAL` means. A path that was already wrong when the command
    was typed is a different condition and gets a different code.
    """

    exit_code = ExitCode.FATAL


class OutputPathError(ConfigError):
    """A path given on the command line is missing or malformed. Nothing ran.

    A `ConfigError` by construction, not by coincidence: "invalid flags, missing
    paths" is that class's whole definition, and a script asking "did I type the
    wrong path, or did something break?" needs the two to answer differently.
    Reported before any logging is configured, because the directory under
    suspicion is usually where the logfile would go.
    """


class UnsafeOutputError(OutputError):
    """Manifest-derived or generated output escaped the export boundary.

    Stays on the fatal branch deliberately. A manifest path that climbs out of
    the export root is not somebody mistyping a flag — it is the export
    contradicting itself, mid-run, about where its own files live.
    """


class PartialOutputError(PaperlessExportError):
    """The exporter succeeded, but requested post-processing was incomplete."""

    exit_code = ExitCode.PARTIAL


class ExporterFailedError(PaperlessExportError):
    """document_exporter ran but failed; carries its original child code."""

    exit_code = ExitCode.FATAL

    def __init__(self, message: str, child_code: int) -> None:
        super().__init__(message)
        self.child_code = child_code
