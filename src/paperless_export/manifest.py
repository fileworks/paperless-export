"""Parse the `manifest.json` written by Paperless-ngx's `document_exporter`.

The manifest is Django dumpdata format: a JSON array of
`{"model": ..., "pk": ..., "fields": {...}}` objects. The exporter annotates
each `documents.document` entry with top-level `__exported_file_name__` /
`__exported_archive_name__` keys pointing at the files it wrote.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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


def _optional_name(
    connection: sqlite3.Connection,
    model: str,
    value: object,
) -> str | None:
    if not isinstance(value, int):
        return None
    row = connection.execute(
        "SELECT name FROM names WHERE model = ? AND pk = ?",
        (model, value),
    ).fetchone()
    return None if row is None else str(row[0])


def _tag_names(connection: sqlite3.Connection, value: object) -> list[str]:
    if not isinstance(value, list):
        raise TypeError
    names: list[str] = []
    for item in value:
        name = _optional_name(connection, "documents.tag", item)
        if name is not None:
            names.append(name)
    return names


def _iter_json_array(path: Path, *, chunk_size: int = 1024 * 1024) -> Iterator[dict[str, Any]]:
    """Incrementally decode a top-level JSON array with bounded input buffering."""
    decoder = json.JSONDecoder()
    try:
        stream = path.open(encoding="utf-8")
    except OSError as exc:
        raise OutputError(f"{path} is not valid JSON: {exc}") from exc
    with stream:
        buffer = ""
        position = 0
        eof = False
        started = False
        expect_value = True
        while True:
            if position:
                buffer = buffer[position:]
                position = 0
            while not eof and len(buffer) < chunk_size:
                chunk = stream.read(chunk_size)
                if chunk:
                    buffer += chunk
                else:
                    eof = True
            stripped = len(buffer) - len(buffer.lstrip())
            position += stripped
            if not started:
                if position >= len(buffer) or buffer[position] != "[":
                    raise OutputError(f"{path} is not valid JSON: expected a top-level array.")
                position += 1
                started = True
                continue
            while position < len(buffer) and buffer[position].isspace():
                position += 1
            if position < len(buffer) and buffer[position] == "]":
                position += 1
                if buffer[position:].strip() or stream.read(1):
                    raise OutputError("manifest.json has trailing data after its JSON array.")
                return
            if not expect_value:
                if position >= len(buffer):
                    if eof:
                        raise OutputError(f"{path} is not valid JSON: truncated array.")
                    continue
                if buffer[position] != ",":
                    raise OutputError(f"{path} is not valid JSON: expected ',' between entries.")
                position += 1
                expect_value = True
                continue
            try:
                value, position = decoder.raw_decode(buffer, position)
            except json.JSONDecodeError as exc:
                if not eof:
                    chunk = stream.read(chunk_size)
                    if chunk:
                        buffer += chunk
                        continue
                    eof = True
                    continue
                raise OutputError(f"{path} is not valid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise OutputError("manifest.json must contain a JSON array of objects.")
            yield value
            expect_value = False


def load_documents(manifest_path: Path) -> list[ExportedDocument]:
    if not manifest_path.is_file():
        raise OutputError(
            f"No manifest.json at {manifest_path}.\n"
            "If document_exporter just reported success, the two paths disagree:\n"
            "  --exporter-target is where the container writes (e.g. ../export)\n"
            "  --export-dir     is that same directory as THIS machine sees it\n"
            "Both must resolve to one folder. Otherwise, run the exporter first."
        )
    root = ExportRoot.from_path(manifest_path.parent)
    with tempfile.NamedTemporaryFile(
        prefix="paperless-manifest-",
        suffix=".sqlite",
        delete=False,
    ) as handle:
        index_path = Path(handle.name)
    connection = sqlite3.connect(index_path)
    try:
        connection.executescript(
            """
            CREATE TABLE names (model TEXT NOT NULL, pk INTEGER NOT NULL, name TEXT NOT NULL,
                                PRIMARY KEY (model, pk));
            CREATE TABLE documents (ordinal INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            """
        )
        for index, entry in enumerate(_iter_json_array(manifest_path)):
            model = entry.get("model")
            if model in {
                "documents.tag",
                "documents.correspondent",
                "documents.documenttype",
            }:
                try:
                    connection.execute(
                        "INSERT OR REPLACE INTO names(model, pk, name) VALUES (?, ?, ?)",
                        (model, int(entry["pk"]), str(entry["fields"]["name"])),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise OutputError(f"manifest.json has a malformed {model} entry.") from exc
                continue
            if model != "documents.document":
                continue
            try:
                identifier: int | str = int(entry["pk"])
            except (KeyError, TypeError, ValueError):
                identifier = f"at index {index}"
            if EXPORTED_FILE_KEY in entry:
                root.confine(
                    document_id=identifier,
                    value=entry.get(EXPORTED_FILE_KEY),
                    field="original",
                )
            if entry.get(EXPORTED_ARCHIVE_KEY) is not None:
                root.confine(
                    document_id=identifier,
                    value=entry.get(EXPORTED_ARCHIVE_KEY),
                    field="archive",
                )
            connection.execute(
                "INSERT INTO documents(ordinal, payload) VALUES (?, ?)",
                (index, json.dumps(entry, separators=(",", ":"))),
            )
        connection.commit()

        documents: list[ExportedDocument] = []
        for index, payload in connection.execute(
            "SELECT ordinal, payload FROM documents ORDER BY ordinal"
        ):
            entry = json.loads(str(payload))
            original_value = entry.get(EXPORTED_FILE_KEY)
            if EXPORTED_FILE_KEY not in entry:
                continue
            try:
                identifier = int(entry["pk"])
                original = root.confine(
                    document_id=identifier,
                    value=original_value,
                    field="original",
                )
                archive_value = entry.get(EXPORTED_ARCHIVE_KEY)
                archive = (
                    root.confine(
                        document_id=identifier,
                        value=archive_value,
                        field="archive",
                    )
                    if archive_value is not None
                    else None
                )
                fields = entry["fields"]
                if not isinstance(fields, dict):
                    raise TypeError
                created_raw = str(fields.get("created") or "")
                documents.append(
                    ExportedDocument(
                        pk=identifier,
                        title=fields.get("title", ""),
                        correspondent=_optional_name(
                            connection,
                            "documents.correspondent",
                            fields.get("correspondent"),
                        ),
                        document_type=_optional_name(
                            connection,
                            "documents.documenttype",
                            fields.get("document_type"),
                        ),
                        tags=_tag_names(connection, fields.get("tags", [])),
                        created=created_raw[:10],
                        original=original,
                        archive=archive,
                    )
                )
            except (KeyError, TypeError, ValidationError) as exc:
                raise OutputError(
                    f"manifest.json has a malformed document at index {index}."
                ) from exc
        return documents
    finally:
        connection.close()
        index_path.unlink(missing_ok=True)
