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

from .errors import OutputError, UnsafeOutputError
from .manifest import ExportedDocument
from .paths import ConfinedPath, ExportRoot

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


def _clear_view(view: ConfinedPath) -> Path:
    lexical = view.root.path / view.relative
    view_root = view.checked()
    if lexical.is_symlink():
        raise UnsafeOutputError("Unsafe generated path for _Steuer: symlinks are forbidden.")
    try:
        if view_root.exists():
            if not view_root.is_dir():
                raise OutputError("_Steuer exists but is not a directory.")
            shutil.rmtree(view_root)
        view_root.mkdir(parents=True)
    except OSError as exc:
        raise OutputError(f"Cannot rebuild _Steuer: {exc.strerror or type(exc).__name__}.") from exc
    return view_root


def validate_tax_view_root(export_dir: Path) -> ConfinedPath:
    """Validate the generated root before any post-processing mutation."""
    root = ExportRoot.from_path(export_dir)
    view = root.generated(Path(TAX_VIEW_DIR), context="_Steuer")
    lexical = root.path / view.relative
    view.checked()
    if lexical.is_symlink():
        raise UnsafeOutputError("Unsafe generated path for _Steuer: symlinks are forbidden.")
    return view


def _unique_name(directory: Path, name: str, pk: int) -> str:
    if not (directory / name).exists() and not (directory / name).is_symlink():
        return name
    stem, dot, suffix = name.rpartition(".")
    return f"{stem}-{pk}.{suffix}" if dot else f"{name}-{pk}"


def _unused_destination(reference: ConfinedPath) -> Path:
    lexical = reference.root.path / reference.relative
    path = reference.checked()
    if lexical.is_symlink() or lexical.exists():
        raise UnsafeOutputError(
            f"Unsafe generated path for {reference.context}: destination changed during "
            "post-processing."
        )
    return path


def build_tax_view(
    export_dir: Path,
    documents: list[ExportedDocument],
    *,
    copy: bool = False,
    prefix: str = DEFAULT_TAG_PREFIX,
) -> TaxViewResult:
    """Create `_Steuer/<YYYY>/` links (or copies) + `_Steuer/INDEX.csv`."""
    view = validate_tax_view_root(export_dir)
    root = view.root
    pattern = tag_pattern(prefix)
    _clear_view(view)

    result = TaxViewResult()
    index_rows: list[tuple[str, str, str, str, str]] = []
    use_copy = copy

    for doc in sorted(documents, key=lambda d: (d.created, d.title)):
        years = doc.tax_years(pattern)
        if not years:
            continue
        source = doc.original.regular_file()
        if source is None:
            result.missing.append(doc.file_path)
            continue
        for year in years:
            year_ref = root.generated(Path(TAX_VIEW_DIR) / year, context=f"_Steuer/{year}")
            year_dir = year_ref.checked()
            try:
                year_dir.mkdir(parents=True, exist_ok=True)
                if (root.path / year_ref.relative).is_symlink():
                    raise UnsafeOutputError(
                        f"Unsafe generated path for _Steuer/{year}: symlinks are forbidden."
                    )
                link_name = _unique_name(year_dir, source.name, doc.pk)
                link_ref = root.generated(
                    Path(TAX_VIEW_DIR) / year / link_name,
                    context=f"_Steuer entry for document {doc.pk}",
                )
                # Repeat the source check immediately before each materialization.
                source = doc.original.regular_file()
                if source is None:
                    result.missing.append(doc.file_path)
                    break
                if use_copy:
                    link = _unused_destination(link_ref)
                    shutil.copy2(source, link)
                    result.copied += 1
                else:
                    try:
                        link = _unused_destination(link_ref)
                        link.symlink_to(doc.original.relative_target_from(link.parent))
                        result.linked += 1
                    except OSError as exc:
                        logger.warning(
                            "Filesystem does not support symlinks (%s) — switching to copies.",
                            exc,
                        )
                        use_copy = True
                        source = doc.original.regular_file()
                        if source is None:
                            result.missing.append(doc.file_path)
                            break
                        link = _unused_destination(link_ref)
                        shutil.copy2(source, link)
                        result.copied += 1
            except UnsafeOutputError:
                raise
            except OSError as exc:
                raise OutputError(
                    f"Cannot materialize confined tax-view output for document {doc.pk}: "
                    f"{exc.strerror or type(exc).__name__}."
                ) from exc
            result.years.add(year)
            index_rows.append(
                (year, doc.title, doc.correspondent or "", doc.created, doc.file_path)
            )

    index_ref = root.generated(Path(TAX_VIEW_DIR) / INDEX_FILE, context="_Steuer index")
    index = index_ref.checked()
    if (root.path / index_ref.relative).is_symlink():
        raise UnsafeOutputError("Unsafe generated path for _Steuer index.")
    temporary_ref = root.generated(
        Path(TAX_VIEW_DIR) / f".{INDEX_FILE}.tmp", context="_Steuer index temporary"
    )
    temporary = _unused_destination(temporary_ref)
    try:
        with temporary.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["year", "title", "correspondent", "created", "original_path"])
            writer.writerows(sorted(index_rows))
        temporary.replace(index)
    except OSError as exc:
        raise OutputError(
            f"Cannot write the confined _Steuer index: {exc.strerror or type(exc).__name__}."
        ) from exc
    return result
