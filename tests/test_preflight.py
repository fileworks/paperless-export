from __future__ import annotations

import httpx
import pytest
import respx

from paperless_export.errors import AuthError, ConfigError, ServerUnreachableError
from paperless_export.preflight import check_api

BASE = "https://paperless.test"


def test_valid_token_passes(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/api/documents/").respond(json={"count": 1, "results": []})
    check_api(BASE, "good-token")


def test_bad_token_raises_auth_error(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/api/documents/").respond(401)
    with pytest.raises(AuthError, match="PAPERLESS_TOKEN"):
        check_api(BASE, "bad-token")


def test_empty_token_raises_auth_error() -> None:
    with pytest.raises(AuthError, match="empty"):
        check_api(BASE, "")


def test_unreachable_raises_clear_error(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/api/documents/").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ServerUnreachableError, match="Cannot reach Paperless"):
        check_api(BASE, "token")


def test_malformed_url_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="http"):
        check_api("paperless.local", "token")
