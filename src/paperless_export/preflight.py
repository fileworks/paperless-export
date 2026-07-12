"""Optional API preflight: fail fast on a bad token or unreachable Paperless.

The exporter itself runs inside the Paperless container and needs no API
access, so this check only runs when a URL is configured.
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from .errors import AuthError, ConfigError, ServerUnreachableError


def check_api(url: str, token: str, *, timeout: float = 15.0) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"$PAPERLESS_URL must be an http(s) URL, got {url!r}.")
    if not token:
        raise AuthError("Authentication failed — check $PAPERLESS_TOKEN (it is empty).")
    try:
        response = httpx.get(
            f"{url.rstrip('/')}/api/documents/",
            params={"page_size": 1},
            headers={"Authorization": f"Token {token}"},
            timeout=timeout,
            follow_redirects=True,
        )
    except httpx.TransportError as exc:
        raise ServerUnreachableError(
            f"Cannot reach Paperless at {url} ({exc}) — is the webserver container running?"
        ) from exc
    if response.status_code in (401, 403):
        raise AuthError("Authentication failed — check $PAPERLESS_TOKEN.")
    if response.status_code >= 400:
        raise ServerUnreachableError(
            f"Paperless at {url} answered {response.status_code} — is it healthy?"
        )
