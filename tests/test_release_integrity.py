"""Release lock, artifact, install, and publication-gate checks."""

from __future__ import annotations

import io
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts.release_integrity import (
    ReleaseIntegrityError,
    _lock_without_project_version,
    artifact_versions,
    lock_version,
    require_new_version,
    source_versions,
)


def _lock(version: str, dependency_version: str = "1.2.3") -> str:
    return (
        "version = 1\nrevision = 3\n"
        "\n[[package]]\n"
        'name = "dependency"\n'
        f'version = "{dependency_version}"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        "\n[[package]]\n"
        'name = "paperless-export"\n'
        f'version = "{version}"\n'
        'source = { editable = "." }\n'
        "dependencies = []\n"
    )


def _artifacts(dist: Path, version: str) -> None:
    dist.mkdir()
    wheel = dist / f"paperless_export-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"paperless_export-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.3\nName: paperless-export\nVersion: {version}\n",
        )
    sdist = dist / f"paperless_export-{version}.tar.gz"
    metadata = (f"Metadata-Version: 2.3\nName: paperless-export\nVersion: {version}\n").encode()
    with tarfile.open(sdist, "w:gz") as archive:
        info = tarfile.TarInfo(f"paperless_export-{version}/PKG-INFO")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))


def test_new_release_must_be_newer_and_match_tag() -> None:
    require_new_version("1.0.1", "v1.0.1")
    with pytest.raises(ReleaseIntegrityError, match="newer"):
        require_new_version("1.0.0", "v1.0.0")
    with pytest.raises(ReleaseIntegrityError, match="does not identify"):
        require_new_version("1.0.1", "v1.0.2")


def test_lock_version_is_part_of_source_identity(tmp_path: Path) -> None:
    (tmp_path / "src/paperless_export").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "paperless-export"\nversion = "0.1.1"\n'
    )
    (tmp_path / "src/paperless_export/__init__.py").write_text('__version__ = "0.1.1"\n')
    (tmp_path / "uv.lock").write_text(_lock("0.1.1"))
    assert source_versions(tmp_path) == {
        "pyproject.toml": "0.1.1",
        "__version__": "0.1.1",
        "uv.lock": "0.1.1",
    }
    assert lock_version(_lock("0.1.1")) == "0.1.1"


def test_lock_guard_allows_only_root_version_change() -> None:
    before = _lock("0.1.0")
    after = _lock("0.1.1")
    assert _lock_without_project_version(before) == _lock_without_project_version(after)
    assert _lock_without_project_version(before) != _lock_without_project_version(
        _lock("0.1.1", dependency_version="2.0.0")
    )


def test_locked_dev_tools_match_their_exact_project_pins() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    declared = {
        name: version
        for requirement in project["dependency-groups"]["dev"]
        for name, version in [requirement.split("==", maxsplit=1)]
    }
    packages = {package["name"]: package for package in lock["package"]}
    locked_requirements = {
        requirement["name"]: requirement["specifier"].removeprefix("==")
        for requirement in packages["paperless-export"]["metadata"]["requires-dev"]["dev"]
    }

    assert locked_requirements == declared
    assert {name: packages[name]["version"] for name in declared} == declared


def test_artifacts_require_exactly_one_consistent_wheel_and_sdist(
    tmp_path: Path,
) -> None:
    dist = tmp_path / "dist"
    _artifacts(dist, "0.1.1")
    assert set(artifact_versions(dist).values()) == {"0.1.1"}
    (dist / "paperless_export-0.1.0.tar.gz").write_bytes(b"stale")
    with pytest.raises(ReleaseIntegrityError, match="exactly one wheel and one sdist"):
        artifact_versions(dist)


def test_release_verification_checks_tagged_lock_and_clean_tree() -> None:
    script = Path("scripts/release_integrity.py").read_text()
    pyproject = Path("pyproject.toml").read_text()
    assert 'f"{tag}:uv.lock"' in script
    assert 'f"{tag}:pyproject.toml"' in script
    assert 'f"{tag}:src/paperless_export/__init__.py"' in script
    assert '"status", "--porcelain", "--untracked-files=no"' in script
    assert '"uv", "lock", "--refresh-package", PROJECT' in script
    assert 'subprocess.run(["git", "add", "uv.lock"]' in script
    assert "python -m ensurepip --upgrade" in pyproject


def test_release_workflow_is_success_and_exact_sha_gated() -> None:
    workflow = Path(".github/workflows/release.yml").read_text()
    assert "workflow_run:" in workflow
    assert "conclusion == 'success'" in workflow
    assert "workflow_run.event == 'push'" in workflow
    assert "workflow_run.head_sha" in workflow
    assert 'test "$(git rev-parse origin/main)" = "$TESTED_SHA"' in workflow
    assert 'test "$(git rev-parse "$RELEASE_COMMIT^")" = "$TESTED_SHA"' in workflow
    assert workflow.index("mkdir -p dist") < workflow.index(
        "Build and stage semantic release locally"
    )


def test_release_verifies_before_every_publication_and_dispatch() -> None:
    workflow = Path(".github/workflows/release.yml").read_text()
    preflight = workflow.index("release_integrity.py preflight")
    build = workflow.index("Build and stage semantic release locally")
    verify = workflow.index("release_integrity.py verify")
    push = workflow.index("git push --atomic")
    github_release = workflow.index("gh release create")
    pypi = workflow.index("pypa/gh-action-pypi-publish@release/v1")
    brew = workflow.index("gh workflow run bump.yml")
    assert preflight < build < verify < push < github_release < pypi < brew
    assert "no_operation_mode: true" in workflow
    assert "push: false" in workflow
    assert "vcs_release: false" in workflow


def test_release_channels_use_explicit_protected_environments() -> None:
    workflow = Path(".github/workflows/release.yml").read_text()

    assert "environment: github-release" in workflow
    assert "environment: pypi" in workflow
    assert "environment: homebrew" in workflow
    assert "name: python-distributions" in workflow
    assert "actions/upload-artifact@v7" in workflow
    assert workflow.count("actions/download-artifact@v8") == 2
    assert workflow.index("environment: github-release") < workflow.index("environment: pypi")
    assert workflow.index("environment: pypi") < workflow.index("environment: homebrew")


def test_locked_ci_and_existing_reviewed_actions_are_preserved() -> None:
    ci = Path(".github/workflows/ci.yml").read_text()
    release = Path(".github/workflows/release.yml").read_text()
    assert "uv lock --check" in ci
    assert "uv sync --locked --all-extras --dev" in ci
    assert "actions/checkout@v7" in ci + release
    assert "python-semantic-release/python-semantic-release@v10" in release
    assert "no_operation_mode: true" in release
    assert "root_options:" not in release
    assert "GH_REPO: ${{ github.repository }}" in release
    assert "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9" in ci


def test_installed_wheel_smoke_includes_pdf_extra_and_cli() -> None:
    script = Path("scripts/release_integrity.py").read_text()
    assert 'f"{wheel.resolve()}[pdf]"' in script
    assert '"import paperless_export, pikepdf"' in script
    assert '[str(executable), "--help"]' in script
    assert '[str(executable), "--version"]' in script


def test_homebrew_dispatch_contract_remains_last() -> None:
    workflow = Path(".github/workflows/release.yml").read_text()
    block = workflow.split("- name: Bump Homebrew formula", maxsplit=1)[1]
    assert '-f "formula=paperless-export"' in block
    assert '-f "version=$RELEASE_VERSION"' in block
    assert '-f "source_repository=$SOURCE_REPOSITORY"' in block
    assert '-f "source_run=$SOURCE_RUN"' in block
