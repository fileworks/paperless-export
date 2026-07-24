from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from paperless_export.embed import embed_metadata
from paperless_export.errors import ConfigError, UnsafeOutputError
from paperless_export.exporter import _run, _SecretRedactor, build_command, run_exporter
from paperless_export.manifest import load_documents
from paperless_export.passphrase import load_passphrase
from paperless_export.taxview import build_tax_view

from .conftest import manifest_entries


def _manifest_with(path: object, *, archive: bool = False) -> list[dict[str, object]]:
    key = "__exported_archive_name__" if archive else "__exported_file_name__"
    return [
        {
            "model": "documents.document",
            "pk": 42,
            "fields": {"title": "unsafe", "tags": [], "created": "2025-01-01"},
            "__exported_file_name__": "safe.pdf",
            key: path,
        }
    ]


def _tax_manifest(path: str) -> list[dict[str, object]]:
    documents = _manifest_with(path)
    fields = documents[0]["fields"]
    assert isinstance(fields, dict)
    fields["tags"] = [1]
    return [
        {"model": "documents.tag", "pk": 1, "fields": {"name": "Steuer-2025"}},
        *documents,
    ]


@pytest.mark.parametrize(
    "unsafe",
    [
        "/private/tmp/outside.pdf",
        r"C:\outside.pdf",
        r"\\server\share\outside.pdf",
        "../outside.pdf",
        "safe/../../outside.pdf",
        "",
        "safe//outside.pdf",
        r"safe\..\outside.pdf",
    ],
)
@pytest.mark.parametrize("archive", [False, True])
def test_manifest_rejects_unsafe_original_and_archive_before_mutation(
    tmp_path: Path, unsafe: str, archive: bool
) -> None:
    export = tmp_path / "export"
    export.mkdir()
    view = export / "_Steuer"
    view.mkdir()
    sentinel = view / "sentinel"
    sentinel.write_text("untouched")
    (export / "manifest.json").write_text(json.dumps(_manifest_with(unsafe, archive=archive)))

    with pytest.raises(UnsafeOutputError, match=r"document 42 (original|archive)"):
        load_documents(export / "manifest.json")
    assert sentinel.read_text() == "untouched"


@pytest.mark.parametrize("archive", [False, True])
def test_manifest_rejects_symlink_escape_for_every_path(tmp_path: Path, archive: bool) -> None:
    export = tmp_path / "export"
    outside = tmp_path / "outside"
    export.mkdir()
    outside.mkdir()
    (outside / "secret.pdf").write_bytes(b"outside")
    (export / "escape").symlink_to(outside, target_is_directory=True)
    (export / "manifest.json").write_text(
        json.dumps(_manifest_with("escape/secret.pdf", archive=archive))
    )
    with pytest.raises(UnsafeOutputError, match="resolves outside"):
        load_documents(export / "manifest.json")
    assert (outside / "secret.pdf").read_bytes() == b"outside"


def test_operation_time_symlink_replacement_is_rejected(tmp_path: Path) -> None:
    export = tmp_path / "export"
    outside = tmp_path / "outside.pdf"
    export.mkdir()
    outside.write_bytes(b"outside")
    entries = _manifest_with("safe.pdf")
    (export / "safe.pdf").write_bytes(b"%PDF-safe")
    (export / "manifest.json").write_text(json.dumps(entries))
    documents = load_documents(export / "manifest.json")

    (export / "safe.pdf").unlink()
    (export / "safe.pdf").symlink_to(outside)
    with pytest.raises(UnsafeOutputError, match="resolves outside"):
        embed_metadata(export, documents)
    assert outside.read_bytes() == b"outside"


def test_tax_view_rejects_operation_time_symlink_replacement(tmp_path: Path) -> None:
    export = tmp_path / "export"
    outside = tmp_path / "outside.pdf"
    export.mkdir()
    outside.write_bytes(b"outside")
    (export / "safe.pdf").write_bytes(b"safe")
    (export / "manifest.json").write_text(json.dumps(_tax_manifest("safe.pdf")))
    documents = load_documents(export / "manifest.json")

    (export / "safe.pdf").unlink()
    (export / "safe.pdf").symlink_to(outside)
    with pytest.raises(UnsafeOutputError, match="resolves outside"):
        build_tax_view(export, documents)
    assert outside.read_bytes() == b"outside"


def test_non_regular_confined_tax_source_is_reported_missing(tmp_path: Path) -> None:
    export = tmp_path / "export"
    export.mkdir()
    (export / "not-a-file.pdf").mkdir()
    (export / "manifest.json").write_text(json.dumps(_tax_manifest("not-a-file.pdf")))
    result = build_tax_view(export, load_documents(export / "manifest.json"))
    assert result.missing == ["not-a-file.pdf"]
    assert "not-a-file.pdf" not in (export / "_Steuer/INDEX.csv").read_text()


def test_unsafe_tax_root_is_rejected_without_outside_cleanup(tmp_path: Path) -> None:
    export = tmp_path / "export"
    outside = tmp_path / "outside"
    export.mkdir()
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("untouched")
    (export / "_Steuer").symlink_to(outside, target_is_directory=True)
    (export / "manifest.json").write_text(json.dumps(manifest_entries()))
    documents = load_documents(export / "manifest.json")

    with pytest.raises(UnsafeOutputError, match="_Steuer"):
        build_tax_view(export, documents)
    assert sentinel.read_text() == "untouched"


def test_passphrase_file_and_stdin_are_supported(tmp_path: Path) -> None:
    source = tmp_path / "passphrase"
    source.write_text("correct horse battery staple\n")
    source.chmod(0o600)
    assert load_passphrase(str(source)) == "correct horse battery staple"

    from io import StringIO

    assert load_passphrase("-", stdin=StringIO("stdin secret\n")) == "stdin secret"


def test_insecure_passphrase_sources_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "passphrase"
    source.write_text("secret")
    if os.name == "posix":
        source.chmod(0o644)
        with pytest.raises(ConfigError, match="0600"):
            load_passphrase(str(source))
        source.chmod(0o600)

    symlink = tmp_path / "passphrase-link"
    symlink.symlink_to(source)
    with pytest.raises(ConfigError, match="must not be a symlink"):
        load_passphrase(str(symlink))
    with pytest.raises(ConfigError, match="not a regular file"):
        load_passphrase(str(tmp_path))


def test_secure_bridge_never_places_secret_in_argv_or_retained_output(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    secret = "do-not-disclose"
    command = build_command(
        "docker compose exec -T webserver document_exporter",
        "../export",
        filename_format=True,
        compare_checksums=True,
        delete=True,
        secure_passphrase=True,
    )
    assert secret not in "\0".join(command)
    assert "--passphrase" not in command
    assert command[-4:] == [
        "../export",
        "--use-filename-format",
        "--compare-checksums",
        "--delete",
    ]

    echo = tmp_path / "echo_stdin.py"
    echo.write_text("import sys; print(sys.stdin.read())")
    completed = _run([sys.executable, str(echo)], stdin_value=secret)
    relayed = capfd.readouterr().err
    assert secret not in relayed
    assert secret not in completed.output
    assert "[REDACTED]" in completed.output


def test_secret_redaction_handles_split_and_overlapping_values() -> None:
    redactor = _SecretRedactor(b"aaaa")
    output = redactor.feed(b"xxaa") + redactor.feed(b"aaa") + redactor.feed(b"", final=True)
    assert output == b"xx[REDACTED]a"
    assert b"aaaa" not in output


def test_custom_command_cannot_receive_passphrase(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    script = tmp_path / "custom.py"
    script.write_text(f"from pathlib import Path; Path({str(marker)!r}).touch()\n")
    with pytest.raises(ConfigError, match="secure adapter"):
        run_exporter(f"{sys.executable} {script}", "/export", passphrase="secret")
    assert not marker.exists()
