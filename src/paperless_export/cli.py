"""Typer CLI: `run` (exporter + views, the nightly job) and `tax-view` (views only)."""

from __future__ import annotations

import importlib.metadata
import logging
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .errors import EXIT_UNEXPECTED, PaperlessExportError, PartialOutputError
from .logging_config import configure_logging, register_secret

app = typer.Typer(add_completion=False, context_settings={"help_option_names": ["-h", "--help"]})


def _version_callback(value: bool) -> None:
    if value:
        try:
            version = importlib.metadata.version("paperless-export")
        except importlib.metadata.PackageNotFoundError:
            version = __version__
        typer.echo(f"paperless-export {version}")
        raise typer.Exit()


@app.callback()
def main(
    _version_flag: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True),
    ] = False,
) -> None:
    """Paperless-ngx export wrapper + _Steuer/YYYY tax view."""


def _guarded[T](
    verbose: bool,
    action: Callable[[], T],
    *,
    log_file: Path,
    secrets: tuple[str, ...] = (),
) -> T:
    for secret in secrets:
        register_secret(secret)
    configure_logging(log_file, verbose=verbose)
    logging.getLogger(__name__).info("paperless-export started; logfile=%s", log_file)
    try:
        result = action()
        logging.getLogger(__name__).info("paperless-export completed successfully")
        return result
    except PaperlessExportError as exc:
        logging.getLogger(__name__).error(
            "paperless-export failed exit_code=%s: %s", exc.exit_code, exc
        )
        if verbose:
            traceback.print_exc()
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    except Exception as exc:
        logging.getLogger(__name__).exception("paperless-export failed unexpectedly")
        if verbose:
            traceback.print_exc()
        typer.secho(
            f"Unexpected error: {exc} (re-run with --verbose for the full traceback)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=EXIT_UNEXPECTED) from exc


def _post_process(
    export_dir: Path,
    *,
    copy: bool,
    tax_tag_prefix: str,
    embed_tags: bool,
    tax_view: bool,
) -> None:
    from .embed import embed_metadata
    from .manifest import load_documents
    from .taxview import build_tax_view, validate_tax_view_root

    reporter = _ProgressReporter()
    reporter("manifest", 0, 0, 0, 0.0, 0.0)
    documents = load_documents(export_dir / "manifest.json")
    reporter("manifest", len(documents), len(documents), 0, 0.0, 0.0)
    if tax_view:
        validate_tax_view_root(export_dir)

    failures: list[str] = []
    if embed_tags:
        embedded = embed_metadata(export_dir, documents, on_progress=reporter)
        typer.echo(
            f"PDF metadata: {embedded.embedded} embedded, "
            f"{embedded.skipped} skipped, {len(embedded.failed)} failed."
        )
        for failed in embedded.failed:
            typer.secho(
                f"  PDF metadata incomplete: {failed}",
                fg=typer.colors.YELLOW,
                err=True,
            )
        failures.extend(f"PDF metadata: {path}" for path in embedded.failed)

    if tax_view:
        result = build_tax_view(
            export_dir,
            documents,
            copy=copy,
            prefix=tax_tag_prefix,
            on_progress=reporter,
        )
        typer.echo(
            f"_Steuer view: {result.total} entries across years "
            f"{', '.join(sorted(result.years)) or '—'} (see _Steuer/INDEX.csv)"
        )
        for missing in result.missing:
            typer.secho(
                f"  missing or non-regular, not materialized: {missing}",
                fg=typer.colors.YELLOW,
                err=True,
            )
        failures.extend(f"_Steuer source: {path}" for path in result.missing)

    if failures:
        joined = "\n".join(f"- {failure}" for failure in failures)
        raise PartialOutputError(
            f"Requested post-processing is incomplete ({len(failures)} failures):\n{joined}"
        )
    typer.echo("Requested post-processing complete.")


class _ProgressReporter:
    """Rate-limited phase progress shared by console and persistent logs."""

    def __init__(self, *, interval_seconds: float = 1.0) -> None:
        self.interval_seconds = interval_seconds
        self._last_emit = 0.0
        self._phase = ""

    def __call__(
        self,
        phase: str,
        current: int,
        total: int,
        failures: int,
        rate: float,
        elapsed: float,
    ) -> None:
        now = time.monotonic()
        if (
            phase == self._phase
            and current != total
            and now - self._last_emit < self.interval_seconds
        ):
            return
        self._phase = phase
        self._last_emit = now
        total_text = str(total) if total else "?"
        message = (
            f"Progress phase={phase} current={current} total={total_text} "
            f"failures={failures} rate={rate:.1f}/s elapsed={elapsed:.1f}s"
        )
        logging.getLogger(__name__).info(message)


@app.command()
def run(
    export_dir: Annotated[
        Path,
        typer.Option(
            "--export-dir", help="Export directory on this host (where manifest.json lands)."
        ),
    ],
    exporter_cmd: Annotated[
        str,
        typer.Option(
            "--exporter-cmd",
            envvar="PAPERLESS_EXPORTER_CMD",
            help="How to invoke document_exporter.",
        ),
    ] = "docker compose exec -T webserver document_exporter",
    exporter_target: Annotated[
        str,
        typer.Option(
            "--exporter-target",
            help="Export path as the exporter process sees it (inside the container).",
        ),
    ] = "../export",
    filename_format: Annotated[
        bool,
        typer.Option(
            "--filename-format/--no-filename-format",
            help="Lay out the export by the storage-path template (--use-filename-format).",
        ),
    ] = True,
    fallback: Annotated[
        bool,
        typer.Option(
            "--fallback/--no-fallback",
            help="On a path-too-long failure, retry as a flat export.",
        ),
    ] = True,
    compare_checksums: Annotated[
        bool,
        typer.Option("--compare-checksums/--no-compare-checksums", help="Incremental re-export."),
    ] = True,
    delete: Annotated[
        bool,
        typer.Option(
            "--delete/--no-delete",
            help="Prune files for documents removed in Paperless (true mirror).",
        ),
    ] = True,
    tax_view: Annotated[
        bool, typer.Option("--tax-view/--no-tax-view", help="Build the _Steuer/YYYY view.")
    ] = True,
    copy: Annotated[
        bool,
        typer.Option("--copy", help="Copy into _Steuer instead of symlinking (FAT/exFAT targets)."),
    ] = False,
    tax_tag_prefix: Annotated[
        str, typer.Option("--tax-tag-prefix", help="Tag prefix marking tax years.")
    ] = "Steuer-",
    embed_tags: Annotated[
        bool,
        typer.Option("--embed-tags", help="Embed tags into the exported PDFs' XMP (needs [pdf])."),
    ] = False,
    passphrase_file: Annotated[
        str,
        typer.Option(
            "--passphrase-file",
            envvar="PAPERLESS_EXPORT_PASSPHRASE_FILE",
            help="Protected passphrase file path; use '-' to read once from stdin.",
        ),
    ] = "",
    url: Annotated[
        str, typer.Option("--url", envvar="PAPERLESS_URL", help="Paperless URL (preflight check).")
    ] = "",
    token: Annotated[
        str, typer.Option("--token", envvar="PAPERLESS_TOKEN", help="Paperless API token.")
    ] = "",
    log_file: Annotated[
        Path | None,
        typer.Option(
            "--log-file",
            envvar="PAPERLESS_EXPORT_LOG_FILE",
            help="Rotating logfile (default: beside the export directory).",
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Run document_exporter, then build the _Steuer/YYYY tax view (the nightly job)."""

    def action() -> None:
        from .exporter import run_exporter
        from .passphrase import load_passphrase
        from .preflight import check_api

        passphrase = load_passphrase(passphrase_file) if passphrase_file else None
        register_secret(passphrase)
        if url:
            check_api(url, token)
        if passphrase is None:
            typer.secho(
                "WARNING: no export passphrase is configured; Paperless may write supported "
                "mail/social-account secret fields to the export in plaintext.",
                fg=typer.colors.YELLOW,
                err=True,
            )
        exporter_progress = _ProgressReporter()
        exporter_progress("native_exporter", 0, 0, 0, 0.0, 0.0)
        result = run_exporter(
            exporter_cmd,
            exporter_target,
            filename_format=filename_format,
            compare_checksums=compare_checksums,
            delete=delete,
            fallback_on_long_paths=fallback,
            passphrase=passphrase,
        )
        exporter_progress("native_exporter", 1, 1, 0, 0.0, 0.0)
        if not result.used_filename_format and filename_format:
            typer.secho(
                "Note: fell back to a flat export (path too long) — see log above.",
                fg=typer.colors.YELLOW,
                err=True,
            )
        typer.echo("document_exporter finished.")
        if tax_view or embed_tags:
            _post_process(
                export_dir,
                copy=copy,
                tax_tag_prefix=tax_tag_prefix,
                embed_tags=embed_tags,
                tax_view=tax_view,
            )

    _guarded(
        verbose,
        action,
        log_file=log_file or export_dir.parent / "paperless-export.log",
        secrets=(token,),
    )


@app.command(name="tax-view")
def tax_view_cmd(
    export_dir: Annotated[
        Path,
        typer.Option("--export-dir", help="Existing export directory containing manifest.json."),
    ],
    copy: Annotated[
        bool,
        typer.Option("--copy", help="Copy into _Steuer instead of symlinking (FAT/exFAT targets)."),
    ] = False,
    tax_tag_prefix: Annotated[
        str, typer.Option("--tax-tag-prefix", help="Tag prefix marking tax years.")
    ] = "Steuer-",
    embed_tags: Annotated[
        bool,
        typer.Option("--embed-tags", help="Embed tags into the exported PDFs' XMP (needs [pdf])."),
    ] = False,
    log_file: Annotated[
        Path | None,
        typer.Option(
            "--log-file",
            envvar="PAPERLESS_EXPORT_LOG_FILE",
            help="Rotating logfile (default: beside the export directory).",
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Rebuild only the _Steuer/YYYY view from an existing export (no exporter run)."""
    _guarded(
        verbose,
        lambda: _post_process(
            export_dir,
            copy=copy,
            tax_tag_prefix=tax_tag_prefix,
            embed_tags=embed_tags,
            tax_view=True,
        ),
        log_file=log_file or export_dir.parent / "paperless-export.log",
    )


if __name__ == "__main__":
    app()
