from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from paperless_export.errors import ServerUnreachableError
from paperless_export.logging_config import configure_logging, sanitize_text, sanitize_url
from paperless_export.preflight import check_api


def test_sanitize_url_removes_userinfo_and_sensitive_query_values() -> None:
    value = "http://url-user:url-password@example.test:8123/path?api_key=query-secret&page=1"

    sanitized = sanitize_url(value)

    assert sanitized == "http://example.test:8123/path?api_key=%5BREDACTED%5D&page=1"
    assert "url-user" not in sanitized
    assert "url-password" not in sanitized
    assert "query-secret" not in sanitized


def test_sanitize_text_redacts_embedded_url_credentials() -> None:
    value = (
        "failed at http://url-user:url-password@example.test/path?token=query-secret after redirect"
    )

    sanitized = sanitize_text(value)

    assert "url-user" not in sanitized
    assert "url-password" not in sanitized
    assert "query-secret" not in sanitized
    assert "example.test/path" in sanitized


def test_failed_preflight_never_logs_url_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "http://url-user:url-password@127.0.0.1:1?api_key=url-query-secret"
    log_file = tmp_path / "paperless-export.log"
    configure_logging(log_file, verbose=False)

    def fail(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError(f"connection refused for {url}")

    monkeypatch.setattr(httpx, "get", fail)
    with pytest.raises(ServerUnreachableError) as captured:
        check_api(url, "header-token")
    logging.shutdown()

    combined = str(captured.value) + log_file.read_text(encoding="utf-8")
    assert "url-user" not in combined
    assert "url-password" not in combined
    assert "url-query-secret" not in combined
