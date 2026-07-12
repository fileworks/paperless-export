"""Thin wrapper around Paperless-ngx's built-in `document_exporter`.

The exporter already does the heavy lifting (type-tree layout via
`--use-filename-format`, incremental `--compare-checksums`, mirror `--delete`,
full `manifest.json`). This module only builds the command, runs it, surfaces
failures honestly, and falls back to a flat export when the filename-format
layout exceeds OS path limits.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass

from .errors import ExporterFailedError, ServerUnreachableError

logger = logging.getLogger(__name__)

DEFAULT_EXPORTER_CMD = "docker compose exec -T webserver document_exporter"
DEFAULT_TARGET = "../export"

_PATH_TOO_LONG_MARKERS = (
    "file name too long",
    "name too long",
    "enametoolong",
    "path too long",
)


@dataclass(frozen=True)
class ExporterRun:
    command: list[str]
    used_filename_format: bool
    stdout: str


def build_command(
    exporter_cmd: str,
    target: str,
    *,
    filename_format: bool,
    compare_checksums: bool,
    delete: bool,
) -> list[str]:
    command = [*shlex.split(exporter_cmd), target]
    if filename_format:
        command.append("--use-filename-format")
    if compare_checksums:
        command.append("--compare-checksums")
    if delete:
        command.append("--delete")
    return command


def _looks_like_path_too_long(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in _PATH_TOO_LONG_MARKERS)


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    logger.info("Running: %s", shlex.join(command))
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise ServerUnreachableError(
            f"Cannot run the exporter: {exc}. Is Docker (or the webserver container) available? "
            "Override the command with --exporter-cmd if Paperless runs differently."
        ) from exc


def run_exporter(
    exporter_cmd: str = DEFAULT_EXPORTER_CMD,
    target: str = DEFAULT_TARGET,
    *,
    filename_format: bool = True,
    compare_checksums: bool = True,
    delete: bool = True,
    fallback_on_long_paths: bool = True,
) -> ExporterRun:
    """Run `document_exporter`; on a path-length failure, retry flat once."""
    command = build_command(
        exporter_cmd,
        target,
        filename_format=filename_format,
        compare_checksums=compare_checksums,
        delete=delete,
    )
    proc = _run(command)
    if proc.returncode == 0:
        return ExporterRun(command, used_filename_format=filename_format, stdout=proc.stdout)

    if filename_format and fallback_on_long_paths and _looks_like_path_too_long(proc.stderr):
        logger.warning(
            "Exporter failed because a path exceeded the OS limit. Falling back to a flat "
            "export (no --use-filename-format) — the folder layout is lost for this run, but "
            "manifest.json still preserves every tag/type/correspondent. Consider shortening "
            "long document titles."
        )
        flat_command = build_command(
            exporter_cmd,
            target,
            filename_format=False,
            compare_checksums=compare_checksums,
            delete=delete,
        )
        flat = _run(flat_command)
        if flat.returncode == 0:
            return ExporterRun(flat_command, used_filename_format=False, stdout=flat.stdout)
        raise ExporterFailedError(
            f"document_exporter failed even without --use-filename-format "
            f"(exit {flat.returncode}):\n{flat.stderr.strip()}",
            flat.returncode,
        )

    raise ExporterFailedError(
        f"document_exporter failed (exit {proc.returncode}):\n{proc.stderr.strip()}",
        proc.returncode,
    )
