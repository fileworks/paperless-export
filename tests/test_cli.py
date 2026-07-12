from __future__ import annotations

import sys
from pathlib import Path

import respx
from typer.testing import CliRunner

from paperless_export.cli import app

runner = CliRunner()


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("paperless-export ")


def test_tax_view_command(export_dir: Path) -> None:
    result = runner.invoke(app, ["tax-view", "--export-dir", str(export_dir)])
    assert result.exit_code == 0
    assert "_Steuer view: 3 documents" in result.output
    assert "2024, 2025" in result.output
    assert (export_dir / "_Steuer/INDEX.csv").is_file()


def test_tax_view_missing_manifest_exits_4(tmp_path: Path) -> None:
    result = runner.invoke(app, ["tax-view", "--export-dir", str(tmp_path)])
    assert result.exit_code == 4
    assert "No manifest" in result.output
    assert "Traceback" not in result.output


def test_run_end_to_end_with_fake_exporter(export_dir: Path, fake_exporter: Path) -> None:
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
    assert result.exit_code == 0
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
    assert result.exit_code == 7
    assert "kaputt" in result.output
    assert "Traceback" not in result.output


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
    assert result.exit_code == 2
    assert "Authentication failed" in result.output
