from __future__ import annotations

import csv
from pathlib import Path

import pytest

from paperless_export.manifest import load_documents
from paperless_export.taxview import build_tax_view


def _index_rows(export_dir: Path) -> list[dict[str, str]]:
    with (export_dir / "_Steuer" / "INDEX.csv").open() as fh:
        return list(csv.DictReader(fh))


class TestBuildTaxView:
    def test_creates_year_dirs_with_symlinks(self, export_dir: Path) -> None:
        docs = load_documents(export_dir / "manifest.json")
        result = build_tax_view(export_dir, docs)

        assert result.linked == 3  # pk10→2024, pk11→2024+2025
        assert result.copied == 0
        assert result.years == {"2024", "2025"}
        assert result.missing == ["Sonstiges/2025-02-02 Verschollen.pdf"]

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
        docs = load_documents(export_dir / "manifest.json")
        build_tax_view(export_dir, docs)
        rows = _index_rows(export_dir)
        assert len(rows) == 3
        assert rows[0]["year"] == "2024"
        assert rows[0]["title"] == "Spanne beider Jahre"
        assert {r["correspondent"] for r in rows} == {"Finanzamt"}

    def test_idempotent_rerun(self, export_dir: Path) -> None:
        docs = load_documents(export_dir / "manifest.json")
        first = build_tax_view(export_dir, docs)
        second = build_tax_view(export_dir, docs)
        assert second.linked == first.linked
        listing = sorted(p.name for p in (export_dir / "_Steuer").rglob("*") if p.is_symlink())
        assert len(listing) == 3  # no -pk suffixed duplicates from the re-run

    def test_copy_mode(self, export_dir: Path) -> None:
        docs = load_documents(export_dir / "manifest.json")
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
        docs = load_documents(export_dir / "manifest.json")
        result = build_tax_view(export_dir, docs)
        assert result.linked == 0
        assert result.copied == 3

    def test_filename_collision_gets_pk_suffix(self, export_dir: Path) -> None:
        docs = load_documents(export_dir / "manifest.json")
        # second doc exporting to the same basename in the same year
        clone = docs[0].model_copy(
            update={"pk": 99, "file_path": "Andere/2024-05-01 Steuerbescheid 2024.pdf"}
        )
        source = export_dir / clone.file_path
        source.parent.mkdir(parents=True)
        source.write_bytes(b"%PDF-fake other")
        result = build_tax_view(export_dir, [*docs, clone])
        year_dir = export_dir / "_Steuer/2024"
        assert (year_dir / "2024-05-01 Steuerbescheid 2024.pdf").exists()
        assert (year_dir / "2024-05-01 Steuerbescheid 2024-99.pdf").exists()
        assert result.linked == 4
