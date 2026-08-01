from __future__ import annotations

import pytest

from paperless_export.progress import snapshot


def test_snapshot_exposes_shared_typed_fields_and_eta() -> None:
    event = snapshot("manifest", 5, 10, 1, 2.5, 2.0)
    assert event.phase == "manifest"
    assert event.current == 5
    assert event.total == 10
    assert event.durable == 4
    assert event.failures == 1
    assert event.eta_seconds == 2.0
    assert "durable=4" in event.render()


def test_snapshot_rejects_unknown_phase() -> None:
    with pytest.raises(ValueError, match="unknown progress phase"):
        snapshot("secret-path", 0, None, 0, 0.0, 0.0)
