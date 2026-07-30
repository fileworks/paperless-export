from __future__ import annotations

import logging
from pathlib import Path

import pytest

import paperless_export.logging_config as logging_config
from paperless_export.logging_config import configure_logging, register_secret


def test_logfile_is_timestamped_rotating_and_redacts_registered_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logfile = tmp_path / "paperless-export.log"
    secret = "scheduled-job-secret"
    monkeypatch.setattr(logging_config, "LOG_MAX_BYTES", 300)
    monkeypatch.setattr(logging_config, "LOG_BACKUPS", 2)
    register_secret(secret)
    configure_logging(logfile, verbose=False)

    logger = logging.getLogger("paperless_export.test")
    for index in range(30):
        logger.info("entry %s %s %s", index, "x" * 40, secret)
    for handler in logging.getLogger().handlers:
        handler.flush()

    files = [logfile, logfile.with_suffix(".log.1")]
    text = "".join(path.read_text() for path in files if path.exists())
    assert "[REDACTED]" in text
    assert secret not in text
    assert "paperless_export.test" in text
    assert logfile.with_suffix(".log.1").exists()
