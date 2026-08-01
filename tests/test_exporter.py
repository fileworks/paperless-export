from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from paperless_export.errors import ExporterFailedError, ServerUnreachableError
from paperless_export.exporter import (
    DIAGNOSTIC_TAIL_BYTES,
    _Completed,
    build_command,
    run_exporter,
)


def _cmd(script: Path) -> str:
    return f"{sys.executable} {script}"


def _completed_run(
    completed: _Completed,
) -> object:
    def run(
        _command: list[str],
        *,
        stdin_value: str | None = None,
        timeout_seconds: float | None = None,
    ) -> _Completed:
        del stdin_value, timeout_seconds
        return completed

    return run


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
        assert excinfo.value.exit_code == 6
        assert excinfo.value.child_code == 5

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

    def test_retains_only_a_bounded_tail_of_large_output(self, tmp_path: Path) -> None:
        script = tmp_path / "large.py"
        script.write_text(f"print('x' * {DIAGNOSTIC_TAIL_BYTES * 2})\n")
        result = run_exporter(_cmd(script), "/export")
        assert len(result.output.encode()) <= DIAGNOSTIC_TAIL_BYTES

    def test_early_path_marker_survives_tail_eviction(self, tmp_path: Path) -> None:
        script = tmp_path / "early_marker.py"
        script.write_text(
            "import json, pathlib, sys\n"
            "if '--use-filename-format' in sys.argv:\n"
            "    print('File name too long')\n"
            f"    print('x' * {DIAGNOSTIC_TAIL_BYTES * 2})\n"
            "    raise SystemExit(1)\n"
            "pathlib.Path(__file__).with_suffix('.argv.json').write_text(json.dumps(sys.argv[1:]))\n"
        )
        result = run_exporter(_cmd(script), "/export")
        assert not result.used_filename_format

    def test_chunk_split_path_marker_triggers_fallback(self, tmp_path: Path) -> None:
        script = tmp_path / "split_marker.py"
        script.write_text(
            "import json, pathlib, sys\n"
            "if '--use-filename-format' in sys.argv:\n"
            "    sys.stdout.buffer.write(b'x' * 8188 + b'file')\n"
            "    sys.stdout.buffer.flush()\n"
            "    sys.stdout.buffer.write(b' name too long')\n"
            "    raise SystemExit(1)\n"
            "pathlib.Path(__file__).with_suffix('.argv.json').write_text(json.dumps(sys.argv[1:]))\n"
        )
        result = run_exporter(_cmd(script), "/export")
        assert not result.used_filename_format

    def test_no_fallback_raises_original_failure(self, tmp_path: Path) -> None:
        script = tmp_path / "toolong.py"
        script.write_text("import sys; sys.stderr.write('File name too long'); sys.exit(1)\n")
        with pytest.raises(ExporterFailedError, match="File name too long"):
            run_exporter(_cmd(script), "/export", fallback_on_long_paths=False)

    def test_missing_binary_is_actionable(self) -> None:
        with pytest.raises(ServerUnreachableError, match="--exporter-cmd"):
            run_exporter("/does/not/exist-binary", "/export")

    def test_silent_child_is_terminated_at_the_configured_timeout(self, tmp_path: Path) -> None:
        script = tmp_path / "silent.py"
        script.write_text("import time; time.sleep(30)\n", encoding="utf-8")

        with pytest.raises(ExporterFailedError, match=r"configured 0\.1s timeout"):
            run_exporter(_cmd(script), "/export", timeout_seconds=0.1)


class TestLiveOutput:
    """A multi-minute export must look alive, not hung."""

    def test_output_is_relayed_while_the_exporter_runs(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        script = tmp_path / "chatty.py"
        script.write_text(
            "import sys\n"
            "print('Exporting document 1 of 2')\n"
            "print('Exporting document 2 of 2')\n"
            "sys.stderr.write('done\\n')\n"
        )
        result = run_exporter(_cmd(script), "/export")

        relayed = capfd.readouterr().err
        assert "Exporting document 1 of 2" in relayed
        assert "Exporting document 2 of 2" in relayed
        # and it is still captured for the caller
        assert "done" in result.output

    def test_path_too_long_on_stdout_still_triggers_the_fallback(self, tmp_path: Path) -> None:
        """Paperless reports this on stdout or stderr depending on version."""
        script = tmp_path / "toolong_stdout.py"
        script.write_text(
            "import json, sys, pathlib\n"
            "if '--use-filename-format' in sys.argv:\n"
            "    print(\"OSError: [Errno 36] File name too long: '/export/x'\")\n"
            "    sys.exit(1)\n"
            "pathlib.Path(__file__).with_suffix('.argv.json').write_text(json.dumps(sys.argv[1:]))\n"
        )
        result = run_exporter(_cmd(script), "/export")

        assert not result.used_filename_format
        argv = json.loads(script.with_suffix(".argv.json").read_text())
        assert "--use-filename-format" not in argv


class TestDockerClassification:
    def test_daemon_project_service_and_container_failures_are_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for output in (
            "Cannot connect to the Docker daemon",
            "no configuration file provided",
            "no such service: webserver",
            "container is not running",
        ):
            completed = _Completed(1, output, False, True)
            monkeypatch.setattr(
                "paperless_export.exporter._run",
                _completed_run(completed),
            )
            with pytest.raises(ServerUnreachableError) as excinfo:
                run_exporter()
            assert excinfo.value.exit_code == 3

    def test_paperless_failure_in_reachable_container_is_exporter_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        completed = _Completed(2, "CommandError: database is locked", False, False)
        monkeypatch.setattr(
            "paperless_export.exporter._run",
            _completed_run(completed),
        )
        with pytest.raises(ExporterFailedError) as excinfo:
            run_exporter()
        assert excinfo.value.exit_code == 6
        assert excinfo.value.child_code == 2
