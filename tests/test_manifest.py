from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperless_export.errors import OutputError
from paperless_export.manifest import load_documents
from paperless_export.taxview import tag_pattern


class TestLoadDocuments:
    def test_resolves_names_from_pks(self, export_dir: Path) -> None:
        docs = {d.pk: d for d in load_documents(export_dir / "manifest.json")}
        assert len(docs) == 4
        assert docs[10].title == "Steuerbescheid 2024"
        assert docs[10].correspondent == "Finanzamt"
        assert docs[10].document_type == "Bescheid"
        assert docs[10].tags == ["Steuer-2024"]
        assert docs[10].created == "2024-05-01"
        assert docs[10].file_path.endswith("Steuerbescheid 2024.pdf")
        assert docs[10].archive_path is not None
        assert docs[12].document_type is None
        assert docs[13].archive_path is None

    def test_skips_entries_without_exported_file(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.json"
        manifest.write_text(
            json.dumps(
                [
                    {
                        "model": "documents.document",
                        "pk": 1,
                        "fields": {"title": "data-only", "tags": []},
                    }
                ]
            )
        )
        assert load_documents(manifest) == []

    def test_missing_manifest_raises(self, tmp_path: Path) -> None:
        with pytest.raises(OutputError, match="No manifest"):
            load_documents(tmp_path / "manifest.json")

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "manifest.json"
        bad.write_text("{not json")
        with pytest.raises(OutputError, match="not valid JSON"):
            load_documents(bad)


class TestTaxYears:
    def test_extracts_years_from_tags(self, export_dir: Path) -> None:
        docs = {d.pk: d for d in load_documents(export_dir / "manifest.json")}
        pattern = tag_pattern()
        assert docs[10].tax_years(pattern) == ["2024"]
        assert docs[11].tax_years(pattern) == ["2024", "2025"]
        assert docs[12].tax_years(pattern) == []

    def test_custom_prefix(self) -> None:
        pattern = tag_pattern("Tax-")
        assert pattern.fullmatch("Tax-2024")
        assert not pattern.fullmatch("Steuer-2024")
