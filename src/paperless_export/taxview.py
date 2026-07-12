"""Materialize the `_Steuer/YYYY/` cross-cutting tax view from the manifest.

The view is derived output, rebuilt from scratch on every run (idempotent).
Cleanup only touches the `_Steuer/` directory itself — never the exported
documents it points at.
"""

from __future__ import annotations

import csv
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import ExportedDocument

logger = logging.getLogger(__name__)

TAX_VIEW_DIR = "_Steuer"
INDEX_FILE = "INDEX.csv"
DEFAULT_TAG_PREFIX = "Steuer-"


def tag_pattern(prefix: str = DEFAULT_TAG_PREFIX) -> re.Pattern[str]:
    return re.compile(rf"{re.escape(prefix)}(\d{{4}})")


@dataclass
class TaxViewResult:
    linked: int = 0
    copied: int = 0
    missing: list[str] = field(default_factory=list)
    years: set[str] = field(default_factory=set)

    @property
    def total(self) -> int:
        return self.linked + self.copied


def _clear_view(view_root: Path) -> None:
    if view_root.exists():
        shutil.rmtree(view_root)
    view_root.mkdir(parents=True)


def _unique_name(directory: Path, name: str, pk: int) -> str:
    if not (directory / name).exists() and not (directory / name).is_symlink():
        return name
    stem, dot, suffix = name.rpartition(".")
    return f"{stem}-{pk}.{suffix}" if dot else f"{name}-{pk}"


def build_tax_view(
    export_dir: Path,
    documents: list[ExportedDocument],
    *,
    copy: bool = False,
    prefix: str = DEFAULT_TAG_PREFIX,
) -> TaxViewResult:
    """Create `_Steuer/<YYYY>/` links (or copies) + `_Steuer/INDEX.csv`."""
    pattern = tag_pattern(prefix)
    view_root = export_dir / TAX_VIEW_DIR
    _clear_view(view_root)

    result = TaxViewResult()
    index_rows: list[tuple[str, str, str, str, str]] = []
    use_copy = copy

    for doc in sorted(documents, key=lambda d: (d.created, d.title)):
        years = doc.tax_years(pattern)
        if not years:
            continue
        source = export_dir / doc.file_path
        if not source.is_file():
            result.missing.append(doc.file_path)
            continue
        for year in years:
            year_dir = view_root / year
            year_dir.mkdir(parents=True, exist_ok=True)
            link = year_dir / _unique_name(year_dir, source.name, doc.pk)
            if use_copy:
                shutil.copy2(source, link)
                result.copied += 1
            else:
                try:
                    link.symlink_to(Path("..") / ".." / doc.file_path)
                    result.linked += 1
                except OSError as exc:
                    logger.warning(
                        "Filesystem does not support symlinks (%s) — switching to copies.", exc
                    )
                    use_copy = True
                    shutil.copy2(source, link)
                    result.copied += 1
            result.years.add(year)
            index_rows.append(
                (year, doc.title, doc.correspondent or "", doc.created, doc.file_path)
            )

    with (view_root / INDEX_FILE).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["year", "title", "correspondent", "created", "original_path"])
        writer.writerows(sorted(index_rows))
    return result
