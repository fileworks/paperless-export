from __future__ import annotations

import json
from pathlib import Path

import pikepdf

from paperless_export.embed import embed_metadata
from paperless_export.manifest import load_documents

from .conftest import manifest_entries


def _make_real_pdfs(export_dir: Path) -> None:
    for entry in manifest_entries():
        name = entry.get("__exported_file_name__")
        if name:
            target = export_dir / str(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            pdf = pikepdf.new()
            pdf.save(target)


def test_embed_writes_xmp_metadata(tmp_path: Path) -> None:
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "manifest.json").write_text(json.dumps(manifest_entries()))
    _make_real_pdfs(export_dir)

    documents = load_documents(export_dir / "manifest.json")
    embedded = embed_metadata(export_dir, documents)
    assert embedded == 4

    pdf_path = export_dir / "Bescheid/Finanzamt/2024-05-01 Steuerbescheid 2024.pdf"
    with pikepdf.open(pdf_path) as pdf, pdf.open_metadata() as meta:
        assert meta["dc:title"] == "Steuerbescheid 2024"
        assert list(meta["dc:subject"]) == ["Steuer-2024"]
        assert list(meta["dc:creator"]) == ["Finanzamt"]


def test_embed_skips_broken_pdf_and_continues(tmp_path: Path) -> None:
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "manifest.json").write_text(json.dumps(manifest_entries()))
    _make_real_pdfs(export_dir)
    # corrupt one of them
    broken = export_dir / "Sonstiges/Allianz/2023-03-03 Haftpflicht Police.pdf"
    broken.write_bytes(b"not a pdf at all")

    documents = load_documents(export_dir / "manifest.json")
    embedded = embed_metadata(export_dir, documents)
    assert embedded == 3
