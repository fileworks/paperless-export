from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


def manifest_entries() -> list[dict[str, Any]]:
    """A realistic document_exporter manifest: tags, correspondents, types, docs."""
    return [
        {"model": "documents.tag", "pk": 1, "fields": {"name": "Steuer-2024"}},
        {"model": "documents.tag", "pk": 2, "fields": {"name": "Steuer-2025"}},
        {"model": "documents.tag", "pk": 3, "fields": {"name": "Versicherung"}},
        {"model": "documents.correspondent", "pk": 1, "fields": {"name": "Finanzamt"}},
        {"model": "documents.correspondent", "pk": 2, "fields": {"name": "Allianz"}},
        {"model": "documents.documenttype", "pk": 1, "fields": {"name": "Bescheid"}},
        {
            "model": "documents.document",
            "pk": 10,
            "fields": {
                "title": "Steuerbescheid 2024",
                "correspondent": 1,
                "document_type": 1,
                "tags": [1],
                "created": "2024-05-01",
            },
            "__exported_file_name__": "Bescheid/Finanzamt/2024-05-01 Steuerbescheid 2024.pdf",
            "__exported_archive_name__": (
                "archive/Bescheid/Finanzamt/2024-05-01 Steuerbescheid 2024.pdf"
            ),
        },
        {
            "model": "documents.document",
            "pk": 11,
            "fields": {
                "title": "Spanne beider Jahre",
                "correspondent": 1,
                "document_type": 1,
                "tags": [1, 2],
                "created": "2025-01-15",
            },
            "__exported_file_name__": "Bescheid/Finanzamt/2025-01-15 Spanne beider Jahre.pdf",
        },
        {
            "model": "documents.document",
            "pk": 12,
            "fields": {
                "title": "Haftpflicht Police",
                "correspondent": 2,
                "document_type": None,
                "tags": [3],
                "created": "2023-03-03",
            },
            "__exported_file_name__": "Sonstiges/Allianz/2023-03-03 Haftpflicht Police.pdf",
        },
        {
            "model": "documents.document",
            "pk": 13,
            "fields": {
                "title": "Verschollen",
                "correspondent": None,
                "document_type": None,
                "tags": [2],
                "created": "2025-02-02",
            },
            "__exported_file_name__": "Sonstiges/2025-02-02 Verschollen.pdf",
        },
    ]


@pytest.fixture
def export_dir(tmp_path: Path) -> Path:
    """Export dir with manifest.json + the exported files (except pk 13, 'missing')."""
    export = tmp_path / "export"
    export.mkdir()
    (export / "manifest.json").write_text(json.dumps(manifest_entries()), encoding="utf-8")
    for rel in [
        "Bescheid/Finanzamt/2024-05-01 Steuerbescheid 2024.pdf",
        "Bescheid/Finanzamt/2025-01-15 Spanne beider Jahre.pdf",
        "Sonstiges/Allianz/2023-03-03 Haftpflicht Police.pdf",
    ]:
        target = export / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-fake " + rel.encode())
    return export


@pytest.fixture
def fake_exporter(tmp_path: Path) -> Path:
    """A stand-in for document_exporter that records its argv and exits 0."""
    script = tmp_path / "fake_exporter.py"
    script.write_text(
        "import json, sys, pathlib\n"
        "pathlib.Path(__file__).with_suffix('.argv.json').write_text(json.dumps(sys.argv[1:]))\n"
    )
    return script
