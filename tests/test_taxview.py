from __future__ import annotations

import csv
from pathlib import Path

import pytest

from paperless_export.errors import OutputError
from paperless_export.manifest import ExportedDocument, load_documents
from paperless_export.paths import ExportRoot
from paperless_export.taxview import (
    JOURNAL_FILE,
    PREVIOUS_PREFIX,
    STAGING_PREFIX,
    build_tax_view,
    recover_tax_view_publication,
)


def _index_rows(export_dir: Path) -> list[dict[str, str]]:
    with (export_dir / "_Steuer" / "INDEX.csv").open() as fh:
        return list(csv.DictReader(fh))


def _complete_documents(export_dir: Path) -> list[ExportedDocument]:
    return [
        document
        for document in load_documents(export_dir / "manifest.json")
        if document.original.regular_file() is not None
    ]


class TestBuildTaxView:
    def test_creates_year_dirs_with_symlinks(self, export_dir: Path) -> None:
        docs = _complete_documents(export_dir)
        result = build_tax_view(export_dir, docs)

        assert result.linked == 3  # pk10→2024, pk11→2024+2025
        assert result.copied == 0
        assert result.years == {"2024", "2025"}
        assert result.missing == []

        link = export_dir / "_Steuer/2024/2024-05-01 Steuerbescheid 2024.pdf"
        assert link.is_symlink()
        assert link.resolve() == (
            export_dir / "Bescheid/Finanzamt/2024-05-01 Steuerbescheid 2024.pdf"
        )
        assert (export_dir / "_Steuer/2024/2025-01-15 Spanne beider Jahre.pdf").is_symlink()
        assert (export_dir / "_Steuer/2025/2025-01-15 Spanne beider Jahre.pdf").is_symlink()
        # non-tax doc is not in the view
        assert not list((export_dir / "_Steuer").rglob("*Haftpflicht*"))

    def test_index_csv_contents(self, export_dir: Path) -> None:
        docs = _complete_documents(export_dir)
        build_tax_view(export_dir, docs)
        rows = _index_rows(export_dir)
        assert len(rows) == 3
        assert rows[0]["year"] == "2024"
        assert rows[0]["title"] == "Spanne beider Jahre"
        assert {r["correspondent"] for r in rows} == {"Finanzamt"}

    def test_idempotent_rerun(self, export_dir: Path) -> None:
        docs = _complete_documents(export_dir)
        first = build_tax_view(export_dir, docs)
        second = build_tax_view(export_dir, docs)
        assert second.linked == first.linked
        listing = sorted(p.name for p in (export_dir / "_Steuer").rglob("*") if p.is_symlink())
        assert len(listing) == 3  # no -pk suffixed duplicates from the re-run

    def test_copy_mode(self, export_dir: Path) -> None:
        docs = _complete_documents(export_dir)
        result = build_tax_view(export_dir, docs, copy=True)
        assert result.copied == 3
        target = export_dir / "_Steuer/2024/2024-05-01 Steuerbescheid 2024.pdf"
        assert target.is_file() and not target.is_symlink()
        assert target.read_bytes().startswith(b"%PDF-fake")

    def test_symlink_failure_falls_back_to_copy(
        self, export_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken_symlink(self: Path, target: object, *args: object) -> None:
            raise OSError("Operation not permitted")

        monkeypatch.setattr(Path, "symlink_to", broken_symlink)
        docs = _complete_documents(export_dir)
        result = build_tax_view(export_dir, docs)
        assert result.linked == 0
        assert result.copied == 3

    def test_filename_collision_gets_pk_suffix(self, export_dir: Path) -> None:
        docs = _complete_documents(export_dir)
        # second doc exporting to the same basename in the same year
        root = ExportRoot.from_path(export_dir)
        clone = docs[0].model_copy(
            update={
                "pk": 99,
                "original": root.confine(
                    "Andere/2024-05-01 Steuerbescheid 2024.pdf",
                    document_id=99,
                    field="original",
                ),
            }
        )
        source = export_dir / clone.file_path
        source.parent.mkdir(parents=True)
        source.write_bytes(b"%PDF-fake other")
        result = build_tax_view(export_dir, [*docs, clone])
        year_dir = export_dir / "_Steuer/2024"
        assert (year_dir / "2024-05-01 Steuerbescheid 2024.pdf").exists()
        assert (year_dir / "2024-05-01 Steuerbescheid 2024-99.pdf").exists()
        assert result.linked == 4

    def test_missing_source_preserves_the_last_complete_published_view(
        self, export_dir: Path
    ) -> None:
        build_tax_view(export_dir, _complete_documents(export_dir), copy=True)
        marker = export_dir / "_Steuer" / "last-complete.txt"
        marker.write_text("keep this view")
        docs = load_documents(export_dir / "manifest.json")

        with pytest.raises(OutputError, match="missing or unreadable"):
            build_tax_view(export_dir, docs, copy=True)

        assert marker.read_text() == "keep this view"
        assert not list(export_dir.glob(f"{STAGING_PREFIX}*"))
        assert not (export_dir / JOURNAL_FILE).exists()

    @pytest.mark.parametrize("phase", ["staging", "old_moved", "new_published"])
    def test_startup_recovers_every_journal_transition(self, export_dir: Path, phase: str) -> None:
        run_id = "a" * 32
        target = export_dir / "_Steuer"
        staging = export_dir / f"{STAGING_PREFIX}{run_id}"
        previous = export_dir / f"{PREVIOUS_PREFIX}{run_id}"
        staging.mkdir()
        (staging / "new.txt").write_text("new")
        if phase == "old_moved":
            previous.mkdir()
            (previous / "old.txt").write_text("old")
        elif phase == "new_published":
            staging.replace(target)
            previous.mkdir()
            (previous / "old.txt").write_text("old")
        else:
            target.mkdir()
            (target / "old.txt").write_text("old")
        (export_dir / JOURNAL_FILE).write_text(
            '{"schema_version": 1, '
            f'"run_id": "{run_id}", "phase": "{phase}", '
            f'"staging": "{STAGING_PREFIX}{run_id}", '
            f'"previous": "{PREVIOUS_PREFIX}{run_id}", "target": "_Steuer"}}'
        )

        recover_tax_view_publication(export_dir)

        assert target.is_dir()
        expected = "new.txt" if phase == "new_published" else "old.txt"
        assert (target / expected).is_file()
        assert not staging.exists()
        assert not previous.exists()
        assert not (export_dir / JOURNAL_FILE).exists()

    def test_copy_failure_cleans_stage_and_preserves_current_view(
        self,
        export_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        build_tax_view(export_dir, _complete_documents(export_dir), copy=True)
        marker = export_dir / "_Steuer" / "old.txt"
        marker.write_text("old")

        def fail_copy(_source: Path, _destination: Path) -> None:
            raise OSError("injected copy failure")

        monkeypatch.setattr("paperless_export.taxview._copy_verified", fail_copy)
        with pytest.raises(OutputError, match="Cannot stage"):
            build_tax_view(export_dir, _complete_documents(export_dir), copy=True)

        assert marker.read_text() == "old"
        assert not list(export_dir.glob(f"{STAGING_PREFIX}*"))

    def test_interruption_during_materialization_preserves_current_view(
        self, export_dir: Path
    ) -> None:
        docs = _complete_documents(export_dir)
        build_tax_view(export_dir, docs, copy=True)
        marker = export_dir / "_Steuer" / "old.txt"
        marker.write_text("old")

        def interrupt(
            phase: str,
            current: int,
            _total: int,
            _failures: int,
            _rate: float,
            _elapsed: float,
        ) -> None:
            if phase == "view_materialization" and current == 1:
                raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            build_tax_view(export_dir, docs, copy=True, on_progress=interrupt)

        assert marker.read_text() == "old"
        assert not list(export_dir.glob(f"{STAGING_PREFIX}*"))

    def test_progress_reports_phase_count_rate_elapsed_and_publication(
        self, export_dir: Path
    ) -> None:
        events: list[tuple[str, int, int, int, float, float]] = []

        build_tax_view(
            export_dir,
            _complete_documents(export_dir),
            copy=True,
            on_progress=lambda *event: events.append(event),
        )

        assert {event[0] for event in events} == {
            "view_preflight",
            "view_materialization",
            "publication",
        }
        assert events[-1][1] == events[-1][2] == 3
        assert all(event[3] == 0 and event[4] >= 0 and event[5] >= 0 for event in events)
