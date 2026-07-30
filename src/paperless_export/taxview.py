"""Recoverable all-or-nothing publication of the ``_Steuer`` tax view."""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from .errors import OutputError, UnsafeOutputError
from .manifest import ExportedDocument
from .paths import ConfinedPath, ExportRoot

logger = logging.getLogger(__name__)

TAX_VIEW_DIR = "_Steuer"
INDEX_FILE = "INDEX.csv"
DEFAULT_TAG_PREFIX = "Steuer-"
JOURNAL_FILE = ".paperless-export-taxview.json"
JOURNAL_TEMP_PREFIX = f"{JOURNAL_FILE}."
LOCK_FILE = ".paperless-export-taxview.lock"
STAGING_PREFIX = "._Steuer.staging."
PREVIOUS_PREFIX = "._Steuer.previous."
COPY_BUFFER_BYTES = 1024 * 1024

PublicationPhase = Literal["staging", "old_moved", "new_published"]
ProgressCallback = Callable[[str, int, int, int, float, float], None]


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


@dataclass(frozen=True)
class _Publication:
    run_id: str
    staging: Path
    previous: Path
    target: Path
    journal: Path


def validate_tax_view_root(export_dir: Path) -> ConfinedPath:
    """Validate the generated root before any post-processing mutation."""
    root = ExportRoot.from_path(export_dir)
    view = root.generated(Path(TAX_VIEW_DIR), context="_Steuer")
    lexical = root.path / view.relative
    view.checked()
    if lexical.is_symlink():
        raise UnsafeOutputError("Unsafe generated path for _Steuer: symlinks are forbidden.")
    return view


def recover_tax_view_publication(export_dir: Path) -> tuple[str, ...]:
    """Recover the one journalled swap and remove confined abandoned stages."""
    view = validate_tax_view_root(export_dir)
    root = view.root.path
    journal = root / JOURNAL_FILE
    notes: list[str] = []
    if journal.exists():
        publication, phase = _read_journal(root, journal)
        if phase == "old_moved":
            if publication.target.exists():
                _remove_tree(publication.previous)
            elif publication.previous.is_dir():
                publication.previous.replace(publication.target)
                _fsync_directory(root)
                notes.append("restored the prior _Steuer view after an interrupted swap")
            else:
                raise OutputError(
                    "Cannot recover _Steuer publication: both current and prior views are absent."
                )
            _remove_tree(publication.staging)
        elif phase == "new_published":
            if not publication.target.is_dir():
                raise OutputError(
                    "Cannot recover _Steuer publication: the published view is absent."
                )
            _remove_tree(publication.previous)
            _remove_tree(publication.staging)
            notes.append("completed cleanup after an interrupted _Steuer publication")
        else:
            _remove_tree(publication.staging)
        journal.unlink(missing_ok=True)
        _fsync_directory(root)

    for candidate in root.glob(f"{STAGING_PREFIX}*"):
        _require_managed_sibling(root, candidate, STAGING_PREFIX)
        _remove_tree(candidate)
        notes.append(f"removed abandoned staging directory {candidate.name}")
    for temporary in root.glob(f"{JOURNAL_TEMP_PREFIX}*.tmp"):
        if temporary.parent != root or temporary.is_symlink() or not temporary.is_file():
            raise UnsafeOutputError(f"Unsafe tax-view journal temporary: {temporary}")
        temporary.unlink()
    return tuple(notes)


def build_tax_view(
    export_dir: Path,
    documents: list[ExportedDocument],
    *,
    copy: bool = False,
    prefix: str = DEFAULT_TAG_PREFIX,
    on_progress: ProgressCallback | None = None,
) -> TaxViewResult:
    """Build a complete sibling stage and publish it through a recoverable swap."""
    view = validate_tax_view_root(export_dir)
    root = view.root
    started = time.monotonic()
    with _publication_lock(root.path):
        for note in recover_tax_view_publication(root.path):
            logger.warning(note)
        publication = _new_publication(root)
        selected = _preflight_sources(documents, tag_pattern(prefix))
        required_bytes = (
            sum(source.stat().st_size for _doc, _years, source in selected) if copy else 0
        )
        available = shutil.disk_usage(root.path).free
        if available < required_bytes:
            raise OutputError(
                f"Cannot stage _Steuer: need {required_bytes} bytes but only "
                f"{available} bytes are available."
            )
        _probe_publication_rename(root.path)
        _progress(on_progress, "view_preflight", 0, len(selected), 0, started)

        result = TaxViewResult()
        try:
            publication.staging.mkdir(mode=0o700)
            _write_journal(publication, "staging")
            _materialize(
                publication,
                selected,
                result,
                copy=copy,
                on_progress=on_progress,
                started=started,
            )
            _validate_stage(publication.staging, result.total)
            _sync_tree(publication.staging)
            _publish(publication)
            _progress(
                on_progress,
                "publication",
                result.total,
                result.total,
                0,
                started,
            )
            return result
        except BaseException:
            _rollback(publication)
            raise


def _preflight_sources(
    documents: list[ExportedDocument],
    pattern: re.Pattern[str],
) -> list[tuple[ExportedDocument, list[str], Path]]:
    selected: list[tuple[ExportedDocument, list[str], Path]] = []
    missing: list[str] = []
    for doc in sorted(documents, key=lambda item: (item.created, item.title)):
        years = doc.tax_years(pattern)
        if not years:
            continue
        source = doc.original.regular_file()
        if source is None:
            missing.append(doc.file_path)
            continue
        try:
            with source.open("rb") as stream:
                stream.read(1)
        except OSError:
            missing.append(doc.file_path)
            continue
        selected.append((doc, years, source))
    if missing:
        paths = "\n".join(f"- {path}" for path in missing)
        raise OutputError(
            "Cannot publish _Steuer because required source files are missing or unreadable:\n"
            f"{paths}"
        )
    return selected


def _materialize(
    publication: _Publication,
    selected: list[tuple[ExportedDocument, list[str], Path]],
    result: TaxViewResult,
    *,
    copy: bool,
    on_progress: ProgressCallback | None,
    started: float,
) -> None:
    index_rows: list[tuple[str, str, str, str, str]] = []
    use_copy = copy
    total = sum(len(years) for _doc, years, _source in selected)
    completed = 0
    for doc, years, source in selected:
        for year in years:
            year_dir = publication.staging / year
            year_dir.mkdir(parents=True, exist_ok=True)
            link_name = _unique_name(year_dir, source.name, doc.pk)
            destination = year_dir / link_name
            source = doc.original.regular_file() or _missing_during_build(doc.file_path)
            try:
                if use_copy:
                    _copy_verified(source, destination)
                    result.copied += 1
                else:
                    try:
                        destination.symlink_to(os.path.relpath(source, destination.parent))
                        if destination.resolve(strict=True) != source.resolve(strict=True):
                            raise OutputError(
                                f"Tax-view link validation failed for document {doc.pk}."
                            )
                        result.linked += 1
                    except OSError as exc:
                        logger.warning(
                            "Filesystem does not support symlinks (%s); using verified copies.",
                            exc,
                        )
                        use_copy = True
                        _copy_verified(source, destination)
                        result.copied += 1
            except OSError as exc:
                raise OutputError(
                    f"Cannot stage tax-view output for document {doc.pk}: "
                    f"{exc.strerror or type(exc).__name__}."
                ) from exc
            result.years.add(year)
            index_rows.append(
                (year, doc.title, doc.correspondent or "", doc.created, doc.file_path)
            )
            completed += 1
            _progress(on_progress, "view_materialization", completed, total, 0, started)

    index = publication.staging / INDEX_FILE
    with index.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["year", "title", "correspondent", "created", "original_path"])
        writer.writerows(sorted(index_rows))
        handle.flush()
        os.fsync(handle.fileno())


def _publish(publication: _Publication) -> None:
    target_existed = publication.target.is_dir()
    if publication.target.exists() and not target_existed:
        raise OutputError("_Steuer exists but is not a directory.")
    try:
        if target_existed:
            publication.target.replace(publication.previous)
            _fsync_directory(publication.target.parent)
        _write_journal(publication, "old_moved")
        publication.staging.replace(publication.target)
        _fsync_directory(publication.target.parent)
        _write_journal(publication, "new_published")
        _remove_tree(publication.previous)
        publication.journal.unlink(missing_ok=True)
        _fsync_directory(publication.target.parent)
    except OSError as exc:
        raise OutputError(
            f"Cannot publish the complete _Steuer view: {exc.strerror or type(exc).__name__}."
        ) from exc


def _rollback(publication: _Publication) -> None:
    try:
        if not publication.target.exists() and publication.previous.is_dir():
            publication.previous.replace(publication.target)
            _fsync_directory(publication.target.parent)
        _remove_tree(publication.staging)
        if publication.target.is_dir():
            _remove_tree(publication.previous)
            publication.journal.unlink(missing_ok=True)
            _fsync_directory(publication.target.parent)
    except OSError:
        logger.exception("Could not finish _Steuer rollback; the journal was retained")


def _new_publication(root: ExportRoot) -> _Publication:
    run_id = uuid.uuid4().hex
    staging = root.generated(Path(f"{STAGING_PREFIX}{run_id}"), context="tax-view staging")
    previous = root.generated(Path(f"{PREVIOUS_PREFIX}{run_id}"), context="prior tax view")
    return _Publication(
        run_id=run_id,
        staging=staging.checked(),
        previous=previous.checked(),
        target=root.generated(Path(TAX_VIEW_DIR), context="_Steuer").checked(),
        journal=root.generated(Path(JOURNAL_FILE), context="tax-view journal").checked(),
    )


def _write_journal(publication: _Publication, phase: PublicationPhase) -> None:
    payload = {
        "schema_version": 1,
        "run_id": publication.run_id,
        "phase": phase,
        "staging": publication.staging.name,
        "previous": publication.previous.name,
        "target": TAX_VIEW_DIR,
    }
    temporary = publication.journal.with_name(
        f"{publication.journal.name}.{publication.run_id}.tmp"
    )
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(publication.journal)
    _fsync_directory(publication.journal.parent)


def _read_journal(root: Path, journal: Path) -> tuple[_Publication, PublicationPhase]:
    try:
        payload = json.loads(journal.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported schema")
        run_id = str(payload["run_id"])
        phase = str(payload["phase"])
        if phase not in {"staging", "old_moved", "new_published"}:
            raise ValueError("invalid phase")
        staging = root / str(payload["staging"])
        previous = root / str(payload["previous"])
        target = root / str(payload["target"])
        _require_managed_sibling(root, staging, STAGING_PREFIX, run_id=run_id)
        _require_managed_sibling(root, previous, PREVIOUS_PREFIX, run_id=run_id)
        if target.parent != root or target.name != TAX_VIEW_DIR:
            raise ValueError("invalid target")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise UnsafeOutputError(
            f"Unsafe or unreadable tax-view publication journal: {exc}"
        ) from exc
    return (
        _Publication(run_id, staging, previous, target, journal),
        cast(PublicationPhase, phase),
    )


def _require_managed_sibling(
    root: Path,
    candidate: Path,
    prefix: str,
    *,
    run_id: str | None = None,
) -> None:
    expected_name = f"{prefix}{run_id}" if run_id is not None else None
    if (
        candidate.parent != root
        or not candidate.name.startswith(prefix)
        or (expected_name is not None and candidate.name != expected_name)
        or candidate.is_symlink()
    ):
        raise UnsafeOutputError(f"Unsafe managed tax-view path: {candidate}")


def _copy_verified(source: Path, destination: Path) -> None:
    before = source.stat()
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=COPY_BUFFER_BYTES)
        writer.flush()
        os.fsync(writer.fileno())
    after = source.stat()
    copied = destination.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ) or copied.st_size != before.st_size:
        destination.unlink(missing_ok=True)
        raise OutputError(f"Source changed while copying into the tax view: {source}")
    shutil.copystat(source, destination, follow_symlinks=False)


def _validate_stage(stage: Path, expected_entries: int) -> None:
    if not stage.is_dir() or not (stage / INDEX_FILE).is_file():
        raise OutputError("The staged tax view is incomplete.")
    entries = sum(1 for path in stage.rglob("*") if path.name != INDEX_FILE and not path.is_dir())
    if entries != expected_entries:
        raise OutputError(
            f"The staged tax view has {entries} entries; expected {expected_entries}."
        )


def _sync_tree(root: Path) -> None:
    for directory, _names, files in os.walk(root):
        current = Path(directory)
        for name in files:
            path = current / name
            if path.is_symlink():
                continue
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        _fsync_directory(current)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _probe_publication_rename(root: Path) -> None:
    """Fail before materialization if sibling-directory rename is unavailable."""
    probe_id = uuid.uuid4().hex
    source = root / f".paperless-export.rename-probe.{probe_id}.source"
    destination = root / f".paperless-export.rename-probe.{probe_id}.destination"
    try:
        source.mkdir(mode=0o700)
        source.replace(destination)
        if not destination.is_dir():
            raise OSError("renamed directory is not visible")
    except OSError as exc:
        raise OutputError(
            "This filesystem cannot provide the same-filesystem directory rename "
            "required for safe _Steuer publication."
        ) from exc
    finally:
        _remove_tree(source)
        _remove_tree(destination)


def _unique_name(directory: Path, name: str, pk: int) -> str:
    if not (directory / name).exists() and not (directory / name).is_symlink():
        return name
    stem, dot, suffix = name.rpartition(".")
    return f"{stem}-{pk}.{suffix}" if dot else f"{name}-{pk}"


def _missing_during_build(display: str) -> Path:
    raise OutputError(f"Required source changed or disappeared while staging: {display}")


def _remove_tree(path: Path) -> None:
    if path.is_symlink():
        raise UnsafeOutputError(f"Refusing to remove managed symlink: {path}")
    if path.exists():
        shutil.rmtree(path)


def _progress(
    callback: ProgressCallback | None,
    phase: str,
    current: int,
    total: int,
    failures: int,
    started: float,
) -> None:
    if callback is None:
        return
    elapsed = max(0.0, time.monotonic() - started)
    callback(phase, current, total, failures, current / elapsed if elapsed else 0.0, elapsed)


@contextmanager
def _publication_lock(root: Path) -> Iterator[None]:
    lock = root / LOCK_FILE
    try:
        handle = lock.open("x", encoding="ascii")
    except FileExistsError as exc:
        if lock.is_symlink() or not lock.is_file():
            raise UnsafeOutputError("Unsafe tax-view publication lock.") from exc
        try:
            pid = int(lock.read_text(encoding="ascii").strip())
        except (OSError, ValueError) as read_error:
            raise OutputError(
                "Cannot validate the existing tax-view publication lock."
            ) from read_error
        if _pid_alive(pid):
            raise OutputError("Another tax-view publication is already running.") from exc
        lock.unlink()
        handle = lock.open("x", encoding="ascii")
    try:
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        handle.close()
        lock.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
