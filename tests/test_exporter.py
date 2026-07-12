from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from paperless_export.errors import ExporterFailedError, ServerUnreachableError
from paperless_export.exporter import build_command, run_exporter


def _cmd(script: Path) -> str:
    return f"{sys.executable} {script}"


class TestBuildCommand:
    def test_all_flags(self) -> None:
        cmd = build_command(
            "docker compose exec -T webserver document_exporter",
            "../export",
            filename_format=True,
            compare_checksums=True,
            delete=True,
        )
        assert cmd == [
            "docker",
            "compose",
            "exec",
            "-T",
            "webserver",
            "document_exporter",
            "../export",
            "--use-filename-format",
            "--compare-checksums",
            "--delete",
        ]

    def test_flags_can_be_disabled(self) -> None:
        cmd = build_command(
            "document_exporter",
            "/export",
            filename_format=False,
            compare_checksums=False,
            delete=False,
        )
        assert cmd == ["document_exporter", "/export"]


class TestRunExporter:
    def test_success_passes_verified_flags(self, fake_exporter: Path) -> None:
        result = run_exporter(_cmd(fake_exporter), "/export")
        assert result.used_filename_format
        argv = json.loads(fake_exporter.with_suffix(".argv.json").read_text())
        assert argv == ["/export", "--use-filename-format", "--compare-checksums", "--delete"]

    def test_nonzero_exit_surfaces_stderr_and_code(self, tmp_path: Path) -> None:
        script = tmp_path / "boom.py"
        script.write_text("import sys; sys.stderr.write('database is locked'); sys.exit(5)\n")
        with pytest.raises(ExporterFailedError, match="database is locked") as excinfo:
            run_exporter(_cmd(script), "/export")
        assert excinfo.value.exit_code == 5

    def test_path_too_long_falls_back_to_flat(self, tmp_path: Path) -> None:
        script = tmp_path / "toolong.py"
        script.write_text(
            "import json, sys, pathlib\n"
            "if '--use-filename-format' in sys.argv:\n"
            "    sys.stderr.write(\"OSError: [Errno 36] File name too long: '/export/x'\")\n"
            "    sys.exit(1)\n"
            "pathlib.Path(__file__).with_suffix('.argv.json').write_text(json.dumps(sys.argv[1:]))\n"
        )
        result = run_exporter(_cmd(script), "/export")
        assert not result.used_filename_format
        argv = json.loads(script.with_suffix(".argv.json").read_text())
        assert "--use-filename-format" not in argv
        assert "--compare-checksums" in argv

    def test_no_fallback_raises_original_failure(self, tmp_path: Path) -> None:
        script = tmp_path / "toolong.py"
        script.write_text("import sys; sys.stderr.write('File name too long'); sys.exit(1)\n")
        with pytest.raises(ExporterFailedError, match="File name too long"):
            run_exporter(_cmd(script), "/export", fallback_on_long_paths=False)

    def test_missing_binary_is_actionable(self) -> None:
        with pytest.raises(ServerUnreachableError, match="--exporter-cmd"):
            run_exporter("/does/not/exist-binary", "/export")
