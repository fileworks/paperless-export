#!/usr/bin/env python3
"""Fail-closed preparation and verification for immutable releases."""

from __future__ import annotations

import argparse
import email
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
import venv
import zipfile
from dataclasses import dataclass
from pathlib import Path

PROJECT = "paperless-export"
DISTRIBUTION = "paperless_export"
RELEASED_FLOOR = "0.1.0"
SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?P<suffix>-(?:0|[1-9]\d*|[0-9A-Za-z-][0-9A-Za-z.-]*))?$"
)


class ReleaseIntegrityError(RuntimeError):
    """A release identity, lock, or uniqueness invariant failed."""


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int
    stable: bool
    suffix: str

    @classmethod
    def parse(cls, raw: str) -> Version:
        match = SEMVER.fullmatch(raw)
        if match is None:
            raise ReleaseIntegrityError(f"Invalid semantic version: {raw!r}")
        suffix = match.group("suffix") or ""
        return cls(
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("patch")),
            not suffix,
            suffix,
        )


def require_new_version(version: str, tag: str) -> None:
    if Version.parse(version) <= Version.parse(RELEASED_FLOOR):
        raise ReleaseIntegrityError(
            f"Selected version {version} must be newer than released {RELEASED_FLOOR}."
        )
    if tag != f"v{version}":
        raise ReleaseIntegrityError(
            f"Semantic-release tag {tag!r} does not identify version {version}."
        )


def tag_exists(tag: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/tags/{tag}"],
        check=False,
    )
    if result.returncode not in (0, 1):
        raise ReleaseIntegrityError(f"Could not inspect existing Git tag {tag}.")
    return result.returncode == 0


def pypi_version_exists(
    version: str, *, base_url: str = f"https://pypi.org/pypi/{PROJECT}"
) -> bool:
    request = urllib.request.Request(
        f"{base_url}/{version}/json",
        headers={"Accept": "application/json", "User-Agent": "paperless-export-release-check"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status == 200:
                return True
            raise ReleaseIntegrityError(
                f"PyPI uniqueness check returned unexpected HTTP {response.status}."
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise ReleaseIntegrityError(
            f"PyPI uniqueness check returned HTTP {exc.code}; refusing publication."
        ) from exc
    except urllib.error.URLError as exc:
        raise ReleaseIntegrityError(
            f"PyPI uniqueness check failed ({exc}); refusing publication."
        ) from exc


def preflight(version: str, tag: str) -> None:
    require_new_version(version, tag)
    if tag_exists(tag):
        raise ReleaseIntegrityError(f"Git tag {tag} already exists.")
    if pypi_version_exists(version):
        raise ReleaseIntegrityError(f"{PROJECT} {version} already exists on PyPI.")


def _editable_lock_package(text: str) -> dict[str, object]:
    raw = tomllib.loads(text)
    packages = raw.get("package")
    if not isinstance(packages, list):
        raise ReleaseIntegrityError("uv.lock has no package records.")
    matches = [
        package
        for package in packages
        if isinstance(package, dict)
        and package.get("name") == PROJECT
        and package.get("source") == {"editable": "."}
    ]
    if len(matches) != 1:
        raise ReleaseIntegrityError(
            "uv.lock must contain exactly one editable paperless-export package."
        )
    return matches[0]


def lock_version(text: str) -> str:
    version = _editable_lock_package(text).get("version")
    if not isinstance(version, str):
        raise ReleaseIntegrityError("The editable paperless-export lock entry has no version.")
    return version


def _lock_without_project_version(text: str) -> str:
    blocks = text.split("\n[[package]]\n")
    matching = [
        index
        for index, block in enumerate(blocks)
        if 'name = "paperless-export"' in block and 'source = { editable = "." }' in block
    ]
    if len(matching) != 1:
        raise ReleaseIntegrityError("Cannot isolate the editable paperless-export lock entry.")
    index = matching[0]
    blocks[index], count = re.subn(
        r'(?m)^version = "[^"]+"$',
        'version = "<PROJECT_VERSION>"',
        blocks[index],
        count=1,
    )
    if count != 1:
        raise ReleaseIntegrityError("Cannot isolate the project version in uv.lock.")
    return "\n[[package]]\n".join(blocks)


def source_versions(root: Path) -> dict[str, str]:
    with (root / "pyproject.toml").open("rb") as file:
        project_version = str(tomllib.load(file)["project"]["version"])
    init_text = (root / "src/paperless_export/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', init_text, re.MULTILINE)
    if match is None:
        raise ReleaseIntegrityError("Package __version__ could not be read.")
    lock = (root / "uv.lock").read_text(encoding="utf-8")
    return {
        "pyproject.toml": project_version,
        "__version__": match.group(1),
        "uv.lock": lock_version(lock),
    }


def prepare_release(root: Path) -> None:
    """Refresh and stage only the editable project version in ``uv.lock``."""
    lock_path = root / "uv.lock"
    before = lock_path.read_text(encoding="utf-8")
    subprocess.run(
        ["uv", "lock", "--refresh-package", PROJECT],
        cwd=root,
        check=True,
    )
    after = lock_path.read_text(encoding="utf-8")
    if _lock_without_project_version(after) != _lock_without_project_version(before):
        lock_path.write_text(before, encoding="utf-8")
        raise ReleaseIntegrityError(
            "Targeted release lock refresh changed unrelated dependency resolution."
        )
    versions = source_versions(root)
    if len(set(versions.values())) != 1:
        lock_path.write_text(before, encoding="utf-8")
        raise ReleaseIntegrityError(
            f"Release source versions disagree after lock refresh: {json.dumps(versions)}"
        )
    subprocess.run(["git", "add", "uv.lock"], cwd=root, check=True)


def tagged_source_versions(tag: str) -> dict[str, str]:
    pyproject = subprocess.run(
        ["git", "show", f"{tag}:pyproject.toml"],
        check=True,
        capture_output=True,
    ).stdout
    init_text = subprocess.run(
        ["git", "show", f"{tag}:src/paperless_export/__init__.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    lock = subprocess.run(
        ["git", "show", f"{tag}:uv.lock"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', init_text, re.MULTILINE)
    if match is None:
        raise ReleaseIntegrityError(f"Package __version__ is missing from tag {tag}.")
    return {
        "tag:pyproject.toml": str(tomllib.loads(pyproject.decode())["project"]["version"]),
        "tag:__version__": match.group(1),
        "tag:uv.lock": lock_version(lock),
    }


def _metadata_version(payload: bytes, *, source: str) -> str:
    value = email.message_from_bytes(payload).get("Version")
    if not value:
        raise ReleaseIntegrityError(f"{source} metadata has no Version field.")
    return value


def artifact_versions(dist: Path) -> dict[str, str]:
    files = sorted(
        path for path in dist.iterdir() if path.is_file() and not path.name.startswith(".")
    )
    if len(files) != 2:
        names = ", ".join(path.name for path in files) or "<empty>"
        raise ReleaseIntegrityError(
            f"{dist} must contain exactly one wheel and one sdist; found: {names}."
        )
    wheels = [path for path in files if path.suffix == ".whl"]
    sdists = [path for path in files if path.name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseIntegrityError("Release staging must contain one wheel and one .tar.gz.")

    with zipfile.ZipFile(wheels[0]) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise ReleaseIntegrityError(f"{wheels[0].name} has ambiguous metadata.")
        wheel_version = _metadata_version(archive.read(metadata_names[0]), source=wheels[0].name)
    with tarfile.open(sdists[0], "r:gz") as archive:
        metadata_members = [
            member
            for member in archive.getmembers()
            if member.name.count("/") == 1 and member.name.endswith("/PKG-INFO")
        ]
        if len(metadata_members) != 1:
            raise ReleaseIntegrityError(f"{sdists[0].name} has ambiguous metadata.")
        extracted = archive.extractfile(metadata_members[0])
        if extracted is None:
            raise ReleaseIntegrityError(f"Cannot read metadata from {sdists[0].name}.")
        sdist_version = _metadata_version(extracted.read(), source=sdists[0].name)
    return {wheels[0].name: wheel_version, sdists[0].name: sdist_version}


def verify_git_release(tag: str, commit: str) -> None:
    tagged_commit = subprocess.run(
        ["git", "rev-list", "-n", "1", tag],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if tagged_commit != commit:
        raise ReleaseIntegrityError(
            f"Tag {tag} points to {tagged_commit}, not release commit {commit}."
        )
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status:
        raise ReleaseIntegrityError(
            "Semantic-release left tracked source changes outside its release commit."
        )


def verify_installed_wheel(wheel: Path, version: str) -> None:
    with tempfile.TemporaryDirectory(prefix="paperless-export-release-") as directory:
        environment = Path(directory)
        venv.EnvBuilder(with_pip=True).create(environment)
        scripts = environment / ("Scripts" if sys.platform == "win32" else "bin")
        python = scripts / ("python.exe" if sys.platform == "win32" else "python")
        executable = scripts / (
            "paperless-export.exe" if sys.platform == "win32" else "paperless-export"
        )
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                f"{wheel.resolve()}[pdf]",
            ],
            check=True,
        )
        subprocess.run(
            [str(python), "-c", "import paperless_export, pikepdf"],
            check=True,
        )
        subprocess.run([str(executable), "--help"], check=True, capture_output=True)
        reported = subprocess.run(
            [str(executable), "--version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    expected = f"paperless-export {version}"
    if reported != expected:
        raise ReleaseIntegrityError(f"Installed CLI reported {reported!r}, expected {expected!r}.")


def verify(
    *,
    root: Path,
    dist: Path,
    version: str,
    expected_version: str,
    tag: str,
    commit: str,
    check_install: bool,
) -> None:
    require_new_version(version, tag)
    if version != expected_version:
        raise ReleaseIntegrityError(
            f"Release changed from preflight {expected_version} to {version}."
        )
    identities = source_versions(root) | tagged_source_versions(tag) | artifact_versions(dist)
    disagreements = {source: value for source, value in identities.items() if value != version}
    if disagreements:
        raise ReleaseIntegrityError(
            f"Release version disagreement: {json.dumps(disagreements, sort_keys=True)}"
        )
    source_names = {
        "pyproject.toml",
        "__version__",
        "uv.lock",
        "tag:pyproject.toml",
        "tag:__version__",
        "tag:uv.lock",
    }
    artifact_names = {name for name in identities if name not in source_names}
    expected_names = {
        f"{DISTRIBUTION}-{version}.tar.gz",
        f"{DISTRIBUTION}-{version}-py3-none-any.whl",
    }
    if artifact_names != expected_names:
        raise ReleaseIntegrityError(
            f"Distribution filenames disagree with {version}: {sorted(artifact_names)}"
        )
    verify_git_release(tag, commit)
    if check_install:
        verify_installed_wheel(next(dist.glob("*.whl")), version)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--root", type=Path, default=Path.cwd())
    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--version", required=True)
    preflight_parser.add_argument("--tag", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--root", type=Path, default=Path.cwd())
    verify_parser.add_argument("--dist", type=Path, default=Path("dist"))
    verify_parser.add_argument("--version", required=True)
    verify_parser.add_argument("--expected-version", required=True)
    verify_parser.add_argument("--tag", required=True)
    verify_parser.add_argument("--commit", required=True)
    verify_parser.add_argument("--skip-install-check", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "prepare":
            prepare_release(args.root)
        elif args.command == "preflight":
            preflight(args.version, args.tag)
        else:
            verify(
                root=args.root,
                dist=args.dist,
                version=args.version,
                expected_version=args.expected_version,
                tag=args.tag,
                commit=args.commit,
                check_install=not args.skip_install_check,
            )
    except (OSError, subprocess.SubprocessError, ReleaseIntegrityError) as exc:
        print(f"release integrity failure: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
