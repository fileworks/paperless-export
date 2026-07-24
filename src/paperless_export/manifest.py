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
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from .errors import OutputError
from .paths import ConfinedPath, ExportRoot

EXPORTED_FILE_KEY = "__exported_file_name__"
EXPORTED_ARCHIVE_KEY = "__exported_archive_name__"


class ExportedDocument(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    pk: int
    title: str
    correspondent: str | None
    document_type: str | None
    tags: list[str]
    created: str
    """ISO date (YYYY-MM-DD) if present, else empty string."""
    original: ConfinedPath
    """Validated path of the exported original."""
    archive: ConfinedPath | None
    """Validated path of the exported PDF/A archive version, if one exists."""

    @property
    def file_path(self) -> str:
        return self.original.display

    @property
    def archive_path(self) -> str | None:
        return self.archive.display if self.archive else None

    def tax_years(self, tag_pattern: re.Pattern[str]) -> list[str]:
        years = []
        for tag in self.tags:
            match = tag_pattern.fullmatch(tag)
            if match:
                years.append(match.group(1))
        return sorted(years)


def _names_by_pk(entries: list[dict[str, Any]], model: str) -> dict[int, str]:
    names: dict[int, str] = {}
    for entry in entries:
        if entry.get("model") != model:
            continue
        try:
            names[int(entry["pk"])] = str(entry["fields"]["name"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OutputError(f"manifest.json has a malformed {model} entry.") from exc
    return names


def _optional_name(names: dict[int, str], value: object) -> str | None:
    return names.get(value) if isinstance(value, int) else None


def _tag_names(tags: dict[int, str], value: object) -> list[str]:
    if not isinstance(value, list):
        raise TypeError
    return [tags[item] for item in value if isinstance(item, int) and item in tags]


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
        raw: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise OutputError(f"{manifest_path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, list) or not all(isinstance(entry, dict) for entry in raw):
        raise OutputError("manifest.json must contain a JSON array of objects.")
    entries = cast(list[dict[str, Any]], raw)
    root = ExportRoot.from_path(manifest_path.parent)

    tags = _names_by_pk(entries, "documents.tag")
    correspondents = _names_by_pk(entries, "documents.correspondent")
    doc_types = _names_by_pk(entries, "documents.documenttype")

    # Validate the complete manifest path set before returning a single
    # document to mutation-capable callers.
    confined: dict[int, tuple[ConfinedPath | None, ConfinedPath | None]] = {}
    for index, entry in enumerate(entries):
        if entry.get("model") != "documents.document":
            continue
        identifier: int | str
        try:
            identifier = int(entry["pk"])
        except (KeyError, TypeError, ValueError):
            identifier = f"at index {index}"

        original_value = entry.get(EXPORTED_FILE_KEY)
        original = (
            root.confine(document_id=identifier, value=original_value, field="original")
            if EXPORTED_FILE_KEY in entry
            else None
        )
        archive_value = entry.get(EXPORTED_ARCHIVE_KEY)
        archive = (
            root.confine(document_id=identifier, value=archive_value, field="archive")
            if EXPORTED_ARCHIVE_KEY in entry and archive_value is not None
            else None
        )
        confined[index] = (original, archive)

    documents: list[ExportedDocument] = []
    for index, entry in enumerate(entries):
        if entry.get("model") != "documents.document":
            continue
        original, archive = confined[index]
        if original is None:
            continue  # e.g. --data-only export: nothing on disk to link
        try:
            fields = entry["fields"]
            if not isinstance(fields, dict):
                raise TypeError
            created_raw = str(fields.get("created") or "")
            documents.append(
                ExportedDocument(
                    pk=entry["pk"],
                    title=fields.get("title", ""),
                    correspondent=_optional_name(correspondents, fields.get("correspondent")),
                    document_type=_optional_name(doc_types, fields.get("document_type")),
                    tags=_tag_names(tags, fields.get("tags", [])),
                    created=created_raw[:10],
                    original=original,
                    archive=archive,
                )
            )
        except (KeyError, TypeError, ValidationError) as exc:
            raise OutputError(f"manifest.json has a malformed document at index {index}.") from exc
    return documents
