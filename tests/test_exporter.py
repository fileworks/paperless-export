from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

from paperless_export.errors import ExporterFailedError, ServerUnreachableError
from paperless_export.exit_codes import ExitCode
from paperless_export.exporter import (
    DIAGNOSTIC_TAIL_BYTES,
    _Completed,
    _stop_child,
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
        assert excinfo.value.exit_code == ExitCode.FATAL
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

    def test_stdout_eof_does_not_bypass_timeout_and_child_is_cleaned_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        children: list[subprocess.Popen[str]] = []
        real_popen = subprocess.Popen

        def observe_child(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        monkeypatch.setattr(subprocess, "Popen", observe_child)
        pid_file = tmp_path / "child.pid"
        finished_file = tmp_path / "child.finished"
        script = tmp_path / "closes_output.py"
        script.write_text(
            "import os, pathlib, sys, time\n"
            f"pid_file = pathlib.Path({json.dumps(str(pid_file))})\n"
            f"finished_file = pathlib.Path({json.dumps(str(finished_file))})\n"
            "pid_file.write_text(str(os.getpid()))\n"
            "os.close(1)\n"
            "os.close(2)\n"
            "time.sleep(3)\n"
            "finished_file.write_text('finished')\n",
            encoding="utf-8",
        )

        started = time.monotonic()
        with pytest.raises(ExporterFailedError, match=r"configured 1s timeout"):
            run_exporter(_cmd(script), "/export", timeout_seconds=1.0)

        assert time.monotonic() - started < 2.5
        assert pid_file.is_file()
        assert not finished_file.exists()
        assert len(children) == 1
        assert children[0].poll() is not None


class _UncooperativeProcess:
    def __init__(self) -> None:
        self.wait_timeouts: list[float | None] = []
        self.terminated = False
        self.killed = False

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, *, timeout: float | None = None) -> None:
        self.wait_timeouts.append(timeout)
        assert timeout is not None
        raise subprocess.TimeoutExpired("test-child", timeout)


def test_child_cleanup_uses_the_shared_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("paperless_export.exporter.time.monotonic", lambda: 99.5)
    process = _UncooperativeProcess()

    _stop_child(cast(subprocess.Popen[bytes], process), deadline=100.0)

    assert process.terminated
    assert process.killed
    assert process.wait_timeouts == [0.5, 0.5]


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
            assert excinfo.value.exit_code == ExitCode.CONFLICT

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
        assert excinfo.value.exit_code == ExitCode.FATAL
        assert excinfo.value.child_code == 2
