from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Never

import httpx
import pikepdf
import pytest
import respx
from typer.testing import CliRunner

from paperless_export.cli import app
from paperless_export.exit_codes import ExitCode
from paperless_export.exporter import ExporterRun

runner = CliRunner()


def _declared_options(*command_path: str) -> set[str]:
    """Every `--option` a command actually declares.

    Read from the command tree rather than from `--help`, because the rendered
    help is Rich's output: it wraps, colours and boxes to the terminal it thinks
    it has. Asserting against it made this a function of the runner's width — it
    passed locally and failed on every CI platform, where the help rendered at 80
    columns and the option names were not in the text at all.

    The contract is "the README documents the options that exist", and the
    declarations are what "exist" means.
    """
    import typer.main

    command: object = typer.main.get_command(app)
    for name in command_path:
        command = getattr(command, "commands", {})[name]
    return {
        opt
        for parameter in getattr(command, "params", [])
        # `secondary_opts` too: a boolean flag declares `--tax-view` in `opts`
        # and `--no-tax-view` here, and the README documents the negative form.
        for opt in (getattr(parameter, "opts", []) or [])
        + (getattr(parameter, "secondary_opts", []) or [])
        if opt.startswith("--")
    }


def test_documented_commands_options_and_environment_aliases_match_cli() -> None:
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    run_options = _declared_options("run")
    tax_options = _declared_options("tax-view")

    for option in (
        "--export-dir",
        "--exporter-cmd",
        "--exporter-target",
        "--passphrase-file",
        "--copy",
        "--no-tax-view",
        "--log-file",
    ):
        assert option in run_options
        assert option in readme
    assert "paperless-export run --export-dir ~/paperless-export" in readme
    assert "PAPERLESS_EXPORT_PASSPHRASE_FILE" in readme
    assert "PAPERLESS_EXPORT_LOG_FILE" in readme
    assert "--copy" in tax_options
    assert "--no-symlinks" not in readme
    assert "--compose-file" not in readme


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == ExitCode.SUCCESS
    assert result.output.startswith("paperless-export ")


def test_tax_view_command(export_dir: Path) -> None:
    result = runner.invoke(app, ["tax-view", "--export-dir", str(export_dir)])
    assert result.exit_code == ExitCode.FATAL
    assert "required source files are missing or unreadable" in result.output
    assert not (export_dir / "_Steuer").exists()


def test_tax_view_missing_manifest_exits_4(tmp_path: Path) -> None:
    result = runner.invoke(app, ["tax-view", "--export-dir", str(tmp_path)])
    assert result.exit_code == ExitCode.FATAL
    assert "No manifest" in result.output
    assert "Traceback" not in result.output


def test_logfile_environment_alias_is_executed(tmp_path: Path) -> None:
    logfile = tmp_path / "logs" / "scheduled.log"

    result = runner.invoke(
        app,
        ["tax-view", "--export-dir", str(tmp_path)],
        env={"PAPERLESS_EXPORT_LOG_FILE": str(logfile)},
    )

    assert result.exit_code == ExitCode.FATAL
    assert logfile.is_file()
    assert "No manifest" in logfile.read_text()


def test_run_end_to_end_with_fake_exporter(export_dir: Path, fake_exporter: Path) -> None:
    missing = export_dir / "Sonstiges/2025-02-02 Verschollen.pdf"
    missing.parent.mkdir(parents=True, exist_ok=True)
    missing.write_bytes(b"%PDF-fake complete")
    result = runner.invoke(
        app,
        [
            "run",
            "--export-dir",
            str(export_dir),
            "--exporter-cmd",
            f"{sys.executable} {fake_exporter}",
            "--exporter-target",
            str(export_dir),
        ],
    )
    assert result.exit_code == ExitCode.SUCCESS
    assert "document_exporter finished." in result.output
    assert (export_dir / "_Steuer/2024").is_dir()


def test_run_exporter_failure_propagates_exit_code(export_dir: Path, tmp_path: Path) -> None:
    script = tmp_path / "boom.py"
    script.write_text("import sys; sys.stderr.write('kaputt'); sys.exit(7)\n")
    result = runner.invoke(
        app,
        [
            "run",
            "--export-dir",
            str(export_dir),
            "--exporter-cmd",
            f"{sys.executable} {script}",
        ],
    )
    assert result.exit_code == ExitCode.FATAL
    assert "child exit 7" in result.output
    assert "kaputt" in result.output
    assert "Traceback" not in result.output


def test_exporter_failure_preserves_existing_tax_view(export_dir: Path, tmp_path: Path) -> None:
    current = export_dir / "_Steuer"
    current.mkdir()
    marker = current / "complete.txt"
    marker.write_text("old")
    script = tmp_path / "boom.py"
    script.write_text("raise SystemExit(7)\n")

    result = runner.invoke(
        app,
        [
            "run",
            "--export-dir",
            str(export_dir),
            "--exporter-cmd",
            f"{sys.executable} {script}",
        ],
    )

    assert result.exit_code == ExitCode.FATAL
    assert marker.read_text() == "old"


def test_run_without_projections_does_not_require_manifest(
    tmp_path: Path, fake_exporter: Path
) -> None:
    empty_export = tmp_path / "empty"
    empty_export.mkdir()
    result = runner.invoke(
        app,
        [
            "run",
            "--export-dir",
            str(empty_export),
            "--exporter-cmd",
            f"{sys.executable} {fake_exporter}",
            "--no-tax-view",
        ],
    )
    assert result.exit_code == ExitCode.SUCCESS
    assert "No manifest" not in result.output
    assert not (empty_export / "_Steuer").exists()


def _single_pdf_export(tmp_path: Path) -> tuple[Path, Path]:
    export = tmp_path / "export"
    export.mkdir()
    relative = Path("docs/invoice.pdf")
    source = export / relative
    source.parent.mkdir()
    pikepdf.new().save(source)
    entries = [
        {"model": "documents.tag", "pk": 1, "fields": {"name": "Steuer-2025"}},
        {
            "model": "documents.document",
            "pk": 1,
            "fields": {
                "title": "Invoice",
                "tags": [1],
                "created": "2025-01-01",
                "correspondent": None,
                "document_type": None,
            },
            "__exported_file_name__": relative.as_posix(),
        },
    ]
    (export / "manifest.json").write_text(json.dumps(entries))
    return export, source


def test_embed_tags_is_independent_of_tax_view(tmp_path: Path, fake_exporter: Path) -> None:
    export, source = _single_pdf_export(tmp_path)
    result = runner.invoke(
        app,
        [
            "run",
            "--export-dir",
            str(export),
            "--exporter-cmd",
            f"{sys.executable} {fake_exporter}",
            "--no-tax-view",
            "--embed-tags",
        ],
    )
    assert result.exit_code == ExitCode.SUCCESS
    assert not (export / "_Steuer").exists()
    with pikepdf.open(source) as pdf, pdf.open_metadata() as metadata:
        assert metadata["dc:title"] == "Invoice"


def test_embedding_precedes_tax_view_copy(tmp_path: Path) -> None:
    export, _source = _single_pdf_export(tmp_path)
    result = runner.invoke(
        app,
        [
            "tax-view",
            "--export-dir",
            str(export),
            "--embed-tags",
            "--copy",
        ],
    )
    assert result.exit_code == ExitCode.SUCCESS
    copy = export / "_Steuer/2025/invoice.pdf"
    with pikepdf.open(copy) as pdf, pdf.open_metadata() as metadata:
        assert metadata["dc:title"] == "Invoice"


def test_cli_embeds_archive_when_original_is_not_pdf(tmp_path: Path, fake_exporter: Path) -> None:
    export = tmp_path / "export"
    export.mkdir()
    original = export / "docs/scan.jpg"
    archive = export / "archive/scan.pdf"
    original.parent.mkdir()
    archive.parent.mkdir()
    original.write_bytes(b"jpeg")
    pikepdf.new().save(archive)
    entries = [
        {
            "model": "documents.document",
            "pk": 1,
            "fields": {
                "title": "Scanned",
                "tags": [],
                "created": "2025-01-01",
                "correspondent": None,
                "document_type": None,
            },
            "__exported_file_name__": "docs/scan.jpg",
            "__exported_archive_name__": "archive/scan.pdf",
        }
    ]
    (export / "manifest.json").write_text(json.dumps(entries))
    result = runner.invoke(
        app,
        [
            "run",
            "--export-dir",
            str(export),
            "--exporter-cmd",
            f"{sys.executable} {fake_exporter}",
            "--no-tax-view",
            "--embed-tags",
        ],
    )
    assert result.exit_code == ExitCode.SUCCESS
    with pikepdf.open(archive) as pdf, pdf.open_metadata() as metadata:
        assert metadata["dc:title"] == "Scanned"


def test_partial_summary_aggregates_pdf_and_tax_failures(export_dir: Path) -> None:
    result = runner.invoke(
        app,
        ["tax-view", "--export-dir", str(export_dir), "--embed-tags"],
    )
    assert result.exit_code == ExitCode.FATAL
    assert "PDF metadata incomplete" in result.output
    assert "required source files are missing or unreadable" in result.output
    assert not (export_dir / "_Steuer").exists()


def test_plaintext_warning_precedes_exporter_start(tmp_path: Path) -> None:
    export = tmp_path / "export"
    export.mkdir()
    script = tmp_path / "starts.py"
    script.write_text("print('CHILD-START', flush=True)\n")
    result = runner.invoke(
        app,
        [
            "run",
            "--export-dir",
            str(export),
            "--exporter-cmd",
            f"{sys.executable} {script}",
            "--no-tax-view",
        ],
    )
    assert result.exit_code == ExitCode.SUCCESS
    assert result.output.index("WARNING: no export passphrase") < result.output.index("CHILD-START")


def test_passphrase_environment_alias_contains_only_a_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "environment-alias-secret"
    source = tmp_path / "passphrase"
    source.write_text(secret)
    source.chmod(0o600)
    captured: dict[str, object] = {}

    def fake_run(*_args: object, **kwargs: object) -> ExporterRun:
        captured.update(kwargs)
        return ExporterRun(["safe-adapter"], True, "")

    monkeypatch.setattr("paperless_export.exporter.run_exporter", fake_run)
    export = tmp_path / "export"
    export.mkdir()
    result = runner.invoke(
        app,
        ["run", "--export-dir", str(export), "--no-tax-view"],
        env={"PAPERLESS_EXPORT_PASSPHRASE_FILE": str(source)},
    )
    assert result.exit_code == ExitCode.SUCCESS
    assert captured["passphrase"] == secret
    assert secret not in result.output
    assert "no export passphrase" not in result.output


def test_run_bad_token_exits_2(export_dir: Path, respx_mock: respx.MockRouter) -> None:
    respx_mock.get("https://paperless.test/api/documents/").respond(401)
    result = runner.invoke(
        app,
        [
            "run",
            "--export-dir",
            str(export_dir),
            "--url",
            "https://paperless.test",
            "--token",
            "bad",
        ],
    )
    assert result.exit_code == ExitCode.USAGE
    assert "Authentication failed" in result.output


def test_run_unreachable_api_exits_3(export_dir: Path, respx_mock: respx.MockRouter) -> None:
    respx_mock.get("https://paperless.test/api/documents/").mock(
        side_effect=httpx.ConnectError("refused")
    )
    result = runner.invoke(
        app,
        [
            "run",
            "--export-dir",
            str(export_dir),
            "--url",
            "https://paperless.test",
            "--token",
            "token",
        ],
    )
    assert result.exit_code == 3
    assert "Cannot reach Paperless" in result.output


def test_unexpected_wrapper_failure_is_fatal(
    export_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> Never:
        raise RuntimeError("unexpected")

    monkeypatch.setattr("paperless_export.exporter.run_exporter", fail)
    result = runner.invoke(app, ["run", "--export-dir", str(export_dir)])
    assert result.exit_code == ExitCode.FATAL
    assert "Unexpected error" in result.output


def test_tax_view_missing_export_dir_is_usage_not_fatal(tmp_path: Path) -> None:
    """Exit `2`, the code the README has always documented for a missing path.

    It was `4`: `ConfigError` said "invalid flags, missing paths" and
    `OutputError` said "unwritable **or missing**", so a missing export
    directory matched both and got the fatal one. `unpacksort` answered `2` for
    the same mistake, and a script driving all three got two different answers.
    """
    result = runner.invoke(app, ["tax-view", "--export-dir", str(tmp_path / "absent")])

    assert result.exit_code == ExitCode.USAGE
    assert "does not exist" in result.output


def test_tax_view_rejects_a_path_that_is_a_file(tmp_path: Path) -> None:
    target = tmp_path / "not-a-directory"
    target.write_text("")

    result = runner.invoke(app, ["tax-view", "--export-dir", str(target)])

    assert result.exit_code == ExitCode.USAGE
    assert "is not a directory" in result.output


def test_run_creates_a_missing_export_dir_beneath_an_existing_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run` produces the export, so the directory not existing yet is normal."""
    monkeypatch.setattr(
        "paperless_export.exporter.run_exporter",
        lambda *_args, **_kwargs: ExporterRun(["adapter"], True, ""),
    )
    target = tmp_path / "export"

    result = runner.invoke(app, ["run", "--export-dir", str(target), "--no-tax-view"])

    assert result.exit_code == ExitCode.SUCCESS
    assert target.is_dir()


def test_run_refuses_to_invent_a_parent_directory(tmp_path: Path) -> None:
    """One mistyped path must not become a new tree that looks like an export."""
    result = runner.invoke(
        app, ["run", "--export-dir", str(tmp_path / "typo" / "export"), "--no-tax-view"]
    )

    assert result.exit_code == ExitCode.USAGE
    assert "does not exist" in result.output


def test_incomplete_post_processing_exits_partial(
    export_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`1` in the exit-code table: the export is usable but a part is missing.

    Metadata embedding is the non-fatal half. The fixture's files are not real
    PDFs, so every embed fails and each one is named — while the export itself
    is complete and worth keeping. That is what separates `1` from `4`: a
    missing *required* tax-view source is fatal, because the view cannot be
    published at all, and this is not that.
    """
    monkeypatch.setattr(
        "paperless_export.exporter.run_exporter",
        lambda *_args, **_kwargs: ExporterRun(["adapter"], True, ""),
    )

    result = runner.invoke(
        app, ["run", "--export-dir", str(export_dir), "--no-tax-view", "--embed-tags"]
    )

    assert result.exit_code == ExitCode.PARTIAL
    assert "incomplete" in result.output


def test_an_interrupted_run_exits_130(export_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shell convention, and the last row of the table."""

    def interrupt(*_args: object, **_kwargs: object) -> Never:
        raise KeyboardInterrupt

    monkeypatch.setattr("paperless_export.exporter.run_exporter", interrupt)

    result = runner.invoke(app, ["run", "--export-dir", str(export_dir)])

    assert result.exit_code == ExitCode.INTERRUPTED
    assert "nothing was left half-written" in result.output
