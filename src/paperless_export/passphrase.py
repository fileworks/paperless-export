"""Load a Paperless export passphrase without putting it in argv or the environment."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import TextIO

from .errors import ConfigError
from .exporter import MAX_PASSPHRASE_BYTES


def _decode(value: bytes) -> str:
    value = value.removesuffix(b"\n").removesuffix(b"\r")
    if len(value) > MAX_PASSPHRASE_BYTES:
        raise ConfigError(
            f"The passphrase exceeds the supported {MAX_PASSPHRASE_BYTES}-byte bound."
        )
    try:
        decoded = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError("The passphrase source must contain UTF-8 text.") from exc
    if not decoded:
        raise ConfigError("The passphrase source is empty.")
    return decoded


def _read_stdin(stream: TextIO) -> str:
    value = stream.read(MAX_PASSPHRASE_BYTES + 2)
    return _decode(value.encode("utf-8"))


def load_passphrase(source: str, *, stdin: TextIO | None = None) -> str:
    """Read once from a protected regular file, or from stdin when source is ``-``."""
    if source == "-":
        return _read_stdin(stdin if stdin is not None else sys.stdin)
    if not source:
        raise ConfigError("The passphrase-file path is empty.")

    path = Path(source)
    if path.is_symlink():
        raise ConfigError(
            f"Cannot open the protected passphrase file {path}: "
            "it must exist and must not be a symlink."
        )
    # Before opening, because Windows cannot open a directory as a descriptor at
    # all: it failed here and reported "must not be a symlink", never reaching
    # the S_ISREG check below that says what is actually wrong. POSIX opens the
    # directory happily and reaches it, so the two platforms disagreed about the
    # message for the same mistake.
    if path.is_dir():
        raise ConfigError(f"The passphrase source {path} is not a regular file.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ConfigError(
            f"Cannot open the protected passphrase file {path}: "
            "it must exist and must not be a symlink."
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"The passphrase source {path} is not a regular file.")
        if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
            raise ConfigError(
                f"The passphrase file {path} is readable or writable by group/others; "
                "set mode 0600."
            )
        value = os.read(descriptor, MAX_PASSPHRASE_BYTES + 2)
    finally:
        os.close(descriptor)
    return _decode(value)
