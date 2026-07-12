"""Optional: embed Paperless tags/correspondent/type into exported PDFs' XMP.

Requires the `pdf` extra (`pipx install 'paperless-export[pdf]'`). Note that
rewriting a PDF changes its checksum, so embedded files are re-exported (and
re-embedded) on the next `--compare-checksums` run — manifest.json already
preserves all metadata, so only enable this if you want tags *inside* the files.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .errors import ConfigError
from .manifest import ExportedDocument

logger = logging.getLogger(__name__)


def embed_metadata(export_dir: Path, documents: list[ExportedDocument]) -> int:
    """Write tags/correspondent/type into each exported PDF; returns count embedded."""
    try:
        import pikepdf
    except ImportError as exc:
        raise ConfigError(
            "--embed-tags needs pikepdf — install with: pipx install 'paperless-export[pdf]'"
        ) from exc

    embedded = 0
    for doc in documents:
        pdf_path = export_dir / doc.file_path
        if pdf_path.suffix.lower() != ".pdf" or not pdf_path.is_file():
            continue
        try:
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
            embedded += 1
        except Exception as exc:  # one broken PDF must not kill the run
            logger.warning("Could not embed metadata into %s: %s", doc.file_path, exc)
    return embedded
