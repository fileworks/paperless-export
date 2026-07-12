"""Typer CLI: `run` (exporter + views, the nightly job) and `tax-view` (views only)."""

from __future__ import annotations

import importlib.metadata
import logging
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .errors import EXIT_UNEXPECTED, PaperlessExportError

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


def _guarded[T](verbose: bool, action: Callable[[], T]) -> T:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s" if verbose else "%(message)s",
        stream=sys.stderr,
    )
    try:
        return action()
    except PaperlessExportError as exc:
        if verbose:
            traceback.print_exc()
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=exc.exit_code) from exc
    except Exception as exc:
        if verbose:
            traceback.print_exc()
        typer.secho(
            f"Unexpected error: {exc} (re-run with --verbose for the full traceback)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=EXIT_UNEXPECTED) from exc


def _build_views(
    export_dir: Path,
    *,
    copy: bool,
    tax_tag_prefix: str,
    embed_tags: bool,
) -> None:
    from .embed import embed_metadata
    from .manifest import load_documents
    from .taxview import build_tax_view

    documents = load_documents(export_dir / "manifest.json")
    result = build_tax_view(export_dir, documents, copy=copy, prefix=tax_tag_prefix)
    typer.echo(
        f"_Steuer view: {result.total} documents across years "
        f"{', '.join(sorted(result.years)) or '—'} (see _Steuer/INDEX.csv)"
    )
    for missing in result.missing:
        typer.secho(f"  missing on disk, not linked: {missing}", fg=typer.colors.YELLOW, err=True)
    if embed_tags:
        embedded = embed_metadata(export_dir, documents)
        typer.echo(f"Embedded metadata into {embedded} PDFs.")


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
    url: Annotated[
        str, typer.Option("--url", envvar="PAPERLESS_URL", help="Paperless URL (preflight check).")
    ] = "",
    token: Annotated[
        str, typer.Option("--token", envvar="PAPERLESS_TOKEN", help="Paperless API token.")
    ] = "",
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Run document_exporter, then build the _Steuer/YYYY tax view (the nightly job)."""

    def action() -> None:
        from .exporter import run_exporter
        from .preflight import check_api

        if url:
            check_api(url, token)
        result = run_exporter(
            exporter_cmd,
            exporter_target,
            filename_format=filename_format,
            compare_checksums=compare_checksums,
            delete=delete,
            fallback_on_long_paths=fallback,
        )
        if not result.used_filename_format and filename_format:
            typer.secho(
                "Note: fell back to a flat export (path too long) — see log above.",
                fg=typer.colors.YELLOW,
                err=True,
            )
        typer.echo("document_exporter finished.")
        if tax_view:
            _build_views(
                export_dir, copy=copy, tax_tag_prefix=tax_tag_prefix, embed_tags=embed_tags
            )

    _guarded(verbose, action)


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
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Rebuild only the _Steuer/YYYY view from an existing export (no exporter run)."""
    _guarded(
        verbose,
        lambda: _build_views(
            export_dir, copy=copy, tax_tag_prefix=tax_tag_prefix, embed_tags=embed_tags
        ),
    )


if __name__ == "__main__":
    app()
