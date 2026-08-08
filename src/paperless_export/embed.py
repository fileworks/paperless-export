"""Optional: embed Paperless tags/correspondent/type into exported PDFs' XMP.

Requires the `pdf` extra (`pipx install 'paperless-export[pdf]'`). Note that
rewriting a PDF changes its checksum, so embedded files are re-exported (and
re-embedded) on the next `--compare-checksums` run — manifest.json already
preserves all metadata, so only enable this if you want tags *inside* the files.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError, UnsafeOutputError
from .manifest import ExportedDocument
from .paths import ConfinedPath

logger = logging.getLogger(__name__)


@dataclass
class EmbedResult:
    embedded: int = 0
    skipped: int = 0
    failed: list[str] = field(default_factory=list)


def _pdf_targets(doc: ExportedDocument) -> list[ConfinedPath]:
    targets = [doc.original]
    if doc.archive is not None:
        targets.append(doc.archive)
    return [target for target in targets if target.relative.suffix.lower() == ".pdf"]


def embed_metadata(
    _export_dir: Path,
    documents: list[ExportedDocument],
    *,
    on_progress: Callable[[str, int, int, int, float, float], None] | None = None,
) -> EmbedResult:
    """Write metadata into each distinct confined original/archive PDF."""
    try:
        import pikepdf
    except ImportError as exc:
        raise ConfigError(
            "--embed-tags needs pikepdf — install with: pipx install 'paperless-export[pdf]'"
        ) from exc

    result = EmbedResult()
    seen: set[tuple[int, int] | Path] = set()
    total = sum(len(_pdf_targets(document)) for document in documents)
    completed = 0
    started = time.monotonic()
    for doc in documents:
        targets = _pdf_targets(doc)
        result.skipped += int(not targets)
        for target in targets:
            identities = target.identities()
            if identities & seen:
                result.skipped += 1
                continue
            seen.update(identities)
            pdf_path = target.regular_file()
            if pdf_path is None:
                result.failed.append(target.display)
                continue
            try:
                # Repeat confinement and file-type checks at the destructive open.
                pdf_path = target.regular_file()
                if pdf_path is None:
                    result.failed.append(target.display)
                    continue
                with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
                    with pdf.open_metadata() as meta:
                        if doc.tags:
                            meta["dc:subject"] = doc.tags
                            meta["pdf:Keywords"] = ", ".join(doc.tags)
                        if doc.title:
                            meta["dc:title"] = doc.title
                        if doc.correspondent:
                            meta["dc:creator"] = [doc.correspondent]
                        if doc.document_type:
                            meta["dc:type"] = [doc.document_type]
                    pdf.save(pdf_path)
                result.embedded += 1
            except UnsafeOutputError:
                raise
            except Exception as exc:  # noqa: BLE001 - one broken PDF must not stop other safe work
                logger.warning(
                    "Could not embed metadata into %s: %s",
                    target.display,
                    type(exc).__name__,
                )
                result.failed.append(target.display)
            completed += 1
            if on_progress is not None:
                elapsed = max(0.0, time.monotonic() - started)
                on_progress(
                    "pdf_metadata",
                    completed,
                    total,
                    len(result.failed),
                    completed / elapsed if elapsed else 0.0,
                    elapsed,
                )
    return result
