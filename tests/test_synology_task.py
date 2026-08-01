from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "synology-task.sh"


@pytest.mark.skipif(os.name == "nt", reason="Synology DSM uses a POSIX shell")
def test_synology_task_uses_restricted_permissions_and_exact_paths(tmp_path: Path) -> None:
    compose = tmp_path / "compose"
    export = tmp_path / "export"
    compose.mkdir()
    export.mkdir()
    capture = tmp_path / "capture"
    executable = tmp_path / "paperless-export"
    executable.write_text(
        "#!/bin/sh\n"
        'umask > "$CAPTURE.umask"\n'
        'pwd > "$CAPTURE.cwd"\n'
        'printf \'%s\\n\' "$@" > "$CAPTURE.args"\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)
    environment = {
        **os.environ,
        "CAPTURE": str(capture),
        "PAPERLESS_COMPOSE_DIR": str(compose),
        "PAPERLESS_EXPORT_DIR": str(export),
        "PAPERLESS_EXPORT_BIN": str(executable),
    }

    subprocess.run(["/bin/sh", str(SCRIPT)], env=environment, check=True)

    assert capture.with_suffix(".umask").read_text().strip() == "0077"
    assert capture.with_suffix(".cwd").read_text().strip() == str(compose)
    assert capture.with_suffix(".args").read_text().splitlines() == [
        "run",
        "--export-dir",
        str(export),
    ]


@pytest.mark.skipif(os.name == "nt", reason="Synology DSM uses a POSIX shell")
def test_synology_task_fails_before_launch_for_relative_paths(tmp_path: Path) -> None:
    result = subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        env={
            **os.environ,
            "PAPERLESS_COMPOSE_DIR": "relative",
            "PAPERLESS_EXPORT_DIR": str(tmp_path),
            "PAPERLESS_EXPORT_BIN": "/missing",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "absolute path" in result.stderr
