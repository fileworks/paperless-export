"""Parse the `manifest.json` written by Paperless-ngx's `document_exporter`.

The manifest is Django dumpdata format: a JSON array of
`{"model": ..., "pk": ..., "fields": {...}}` objects. The exporter annotates
each `documents.document` entry with top-level `__exported_file_name__` /
`__exported_archive_name__` keys pointing at the files it wrote.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from .errors import OutputError

EXPORTED_FILE_KEY = "__exported_file_name__"
EXPORTED_ARCHIVE_KEY = "__exported_archive_name__"


class ExportedDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    pk: int
    title: str
    correspondent: str | None
    document_type: str | None
    tags: list[str]
    created: str
    """ISO date (YYYY-MM-DD) if present, else empty string."""
    file_path: str
    """Path of the exported original, relative to the export dir."""
    archive_path: str | None
    """Path of the exported PDF/A archive version, if one exists."""

    def tax_years(self, tag_pattern: re.Pattern[str]) -> list[str]:
        years = []
        for tag in self.tags:
            match = tag_pattern.fullmatch(tag)
            if match:
                years.append(match.group(1))
        return sorted(years)


def _names_by_pk(entries: list[dict[str, Any]], model: str) -> dict[int, str]:
    return {
        entry["pk"]: entry["fields"]["name"] for entry in entries if entry.get("model") == model
    }


def load_documents(manifest_path: Path) -> list[ExportedDocument]:
    if not manifest_path.is_file():
        raise OutputError(
            f"No manifest.json at {manifest_path}.\n"
            "If document_exporter just reported success, the two paths disagree:\n"
            "  --exporter-target is where the container writes (e.g. ../export)\n"
            "  --export-dir     is that same directory as THIS machine sees it\n"
            "Both must resolve to one folder. Otherwise, run the exporter first."
        )
    try:
        entries: list[dict[str, Any]] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OutputError(f"{manifest_path} is not valid JSON: {exc}") from exc

    tags = _names_by_pk(entries, "documents.tag")
    correspondents = _names_by_pk(entries, "documents.correspondent")
    doc_types = _names_by_pk(entries, "documents.documenttype")

    documents: list[ExportedDocument] = []
    for entry in entries:
        if entry.get("model") != "documents.document":
            continue
        fields = entry["fields"]
        file_path = entry.get(EXPORTED_FILE_KEY)
        if not file_path:
            continue  # e.g. --data-only export: nothing on disk to link
        created_raw = str(fields.get("created") or "")
        documents.append(
            ExportedDocument(
                pk=entry["pk"],
                title=fields.get("title", ""),
                correspondent=correspondents.get(fields.get("correspondent")),
                document_type=doc_types.get(fields.get("document_type")),
                tags=[tags[pk] for pk in fields.get("tags", []) if pk in tags],
                created=created_raw[:10],
                file_path=file_path,
                archive_path=entry.get(EXPORTED_ARCHIVE_KEY),
            )
        )
    return documents
