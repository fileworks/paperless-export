"""Production manifest-index scale gate with high-cardinality lookup tables."""

from __future__ import annotations

import json
import os
import tracemalloc
from pathlib import Path
from typing import TextIO

from paperless_export.manifest import load_documents

PR_ENTRY_COUNT = 20_000
PEAK_PYTHON_MEMORY_BYTES = 64 * 1024 * 1024


def _write_entry(stream: TextIO, entry: dict[str, object], *, first: bool) -> None:
    if not first:
        stream.write(",")
    json.dump(entry, stream, separators=(",", ":"))


def test_external_name_index_scales_without_manifest_materialization(tmp_path: Path) -> None:
    count = int(os.getenv("PAPERLESS_EXPORT_SCALE_ENTRIES", str(PR_ENTRY_COUNT)))
    original = tmp_path / "documents" / "originals" / "one.pdf"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"%PDF")
    manifest = tmp_path / "manifest.json"
    with manifest.open("w", encoding="utf-8") as stream:
        stream.write("[")
        _write_entry(
            stream,
            {
                "model": "documents.document",
                "pk": 1,
                "fields": {
                    "title": "one",
                    "tags": [count - 1],
                    "correspondent": None,
                    "document_type": None,
                },
                "__exported_file_name__": "documents/originals/one.pdf",
            },
            first=True,
        )
        for index in range(count):
            _write_entry(
                stream,
                {
                    "model": "documents.tag",
                    "pk": index,
                    "fields": {"name": f"tag-{index}"},
                },
                first=False,
            )
        stream.write("]")

    tracemalloc.start()
    try:
        documents = load_documents(manifest)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert documents[0].tags == [f"tag-{count - 1}"]
    assert peak < PEAK_PYTHON_MEMORY_BYTES
