from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pikepdf
import pytest

from paperless_export.embed import embed_metadata
from paperless_export.manifest import load_documents

from .conftest import manifest_entries


def _make_real_pdfs(export_dir: Path) -> None:
    for entry in manifest_entries():
        for key in ("__exported_file_name__", "__exported_archive_name__"):
            name = entry.get(key)
            if name:
                target = export_dir / str(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                pdf = pikepdf.new()
                pdf.add_blank_page(page_size=(72, 72))
                pdf.save(target)


def test_embed_writes_xmp_metadata(tmp_path: Path) -> None:
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "manifest.json").write_text(json.dumps(manifest_entries()))
    _make_real_pdfs(export_dir)

    documents = load_documents(export_dir / "manifest.json")
    result = embed_metadata(export_dir, documents)
    assert result.embedded == 5
    assert result.failed == []

    pdf_path = export_dir / "Bescheid/Finanzamt/2024-05-01 Steuerbescheid 2024.pdf"
    with pikepdf.open(pdf_path) as pdf, pdf.open_metadata() as meta:
        assert meta["dc:title"] == "Steuerbescheid 2024"
        assert list(meta["dc:subject"]) == ["Steuer-2024"]
        assert list(meta["dc:creator"]) == ["Finanzamt"]


def test_poppler_consumer_reads_embedded_title_author_and_keywords(tmp_path: Path) -> None:
    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo is None:
        pytest.skip("pdfinfo is installed in the independent-consumer CI gate")
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "manifest.json").write_text(json.dumps(manifest_entries()))
    _make_real_pdfs(export_dir)
    documents = load_documents(export_dir / "manifest.json")

    result = embed_metadata(export_dir, documents)
    target = export_dir / "Bescheid/Finanzamt/2024-05-01 Steuerbescheid 2024.pdf"
    observed = subprocess.run(
        [pdfinfo, str(target)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert result.failed == []
    assert "Title:           Steuerbescheid 2024" in observed
    assert "Author:          Finanzamt" in observed
    assert "Keywords:        Steuer-2024" in observed


def test_embed_skips_broken_pdf_and_continues(tmp_path: Path) -> None:
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "manifest.json").write_text(json.dumps(manifest_entries()))
    _make_real_pdfs(export_dir)
    # corrupt one of them
    broken = export_dir / "Sonstiges/Allianz/2023-03-03 Haftpflicht Police.pdf"
    broken.write_bytes(b"not a pdf at all")

    documents = load_documents(export_dir / "manifest.json")
    result = embed_metadata(export_dir, documents)
    assert result.embedded == 4
    assert result.failed == ["Sonstiges/Allianz/2023-03-03 Haftpflicht Police.pdf"]


def test_duplicate_original_and_archive_is_embedded_once(tmp_path: Path) -> None:
    entries = manifest_entries()[:]
    document = next(entry for entry in entries if entry.get("pk") == 10)
    document["__exported_archive_name__"] = document["__exported_file_name__"]
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "manifest.json").write_text(json.dumps(entries))
    _make_real_pdfs(export_dir)

    result = embed_metadata(export_dir, load_documents(export_dir / "manifest.json"))
    assert result.embedded == 4
    assert result.skipped == 1
