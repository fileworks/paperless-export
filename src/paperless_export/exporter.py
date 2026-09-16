"""Bounded, Docker-aware wrapper around Paperless's document exporter."""

from __future__ import annotations

import codecs
import contextlib
import logging
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import NoReturn

from .errors import ConfigError, ExporterFailedError, ServerUnreachableError
from .logging_config import sanitize_text
from .progress import snapshot

logger = logging.getLogger(__name__)
transcript_logger = logging.getLogger(f"{__name__}.transcript")

DEFAULT_EXPORTER_CMD = "docker compose exec -T webserver document_exporter"
DEFAULT_TARGET = "../export"
DIAGNOSTIC_TAIL_BYTES = 64 * 1024
STREAM_CHUNK_BYTES = 8 * 1024
MAX_PASSPHRASE_BYTES = 4096
DEFAULT_EXPORT_TIMEOUT_SECONDS = 6 * 60 * 60
HEARTBEAT_SECONDS = 5.0


def split_command(command: str) -> list[str]:
    """Split a command string into argv, portably.

    `shlex.split` defaults to POSIX rules, where a backslash escapes the next
    character. On Windows that silently destroys every path it is given:
    `C:\\Python\\python.exe` came back as `C:Pythonpython.exe`, so the exporter
    could not be launched at all and every run exited 3.

    Non-POSIX mode keeps backslashes but leaves quotes attached to the tokens it
    produces, so they are stripped here.
    """
    if os.name == "nt":
        return [token.strip('"') for token in shlex.split(command, posix=False)]
    return shlex.split(command)


_DEFAULT_EXPORTER_TOKENS = split_command(DEFAULT_EXPORTER_CMD)
_PASSPHRASE_BRIDGE = (
    "import sys,django;"
    "django.setup();"
    "from django.core.management import call_command;"
    "secret=sys.stdin.read().removesuffix('\\n').removesuffix('\\r');"
    "call_command('document_exporter',*sys.argv[1:],passphrase=secret)"
)
_PATH_TOO_LONG_MARKERS = (
    b"file name too long",
    b"name too long",
    b"enametoolong",
    b"path too long",
)
_DOCKER_UNAVAILABLE_MARKERS = (
    b"cannot connect to the docker daemon",
    b"is the docker daemon running",
    b"no configuration file provided",
    b"can't find a suitable configuration file",
    b"no such service",
    b"no such container",
    b"no container found",
    b"container is not running",
    b"service is not running",
)
_MAX_MARKER_BYTES = max(map(len, (*_PATH_TOO_LONG_MARKERS, *_DOCKER_UNAVAILABLE_MARKERS)))


@dataclass(frozen=True)
class ExporterRun:
    command: list[str]
    used_filename_format: bool
    output: str
    """The bounded diagnostic tail, not the complete streamed transcript."""


@dataclass(frozen=True)
class _Completed:
    returncode: int
    output: str
    path_too_long: bool
    docker_unavailable: bool


@dataclass
class _Signals:
    overlap: bytes = b""
    path_too_long: bool = False
    docker_unavailable: bool = False

    def observe(self, chunk: bytes) -> None:
        window = (self.overlap + chunk).lower()
        self.path_too_long = self.path_too_long or any(
            marker in window for marker in _PATH_TOO_LONG_MARKERS
        )
        self.docker_unavailable = self.docker_unavailable or any(
            marker in window for marker in _DOCKER_UNAVAILABLE_MARKERS
        )
        self.overlap = window[-(_MAX_MARKER_BYTES - 1) :]


@dataclass
class _SecretRedactor:
    secret: bytes | None
    pending: bytes = b""

    def feed(self, chunk: bytes, *, final: bool = False) -> bytes:
        if not self.secret:
            return chunk
        output = bytearray()
        for value in chunk:
            self.pending += bytes((value,))
            while self.pending and not self.secret.startswith(self.pending):
                output.append(self.pending[0])
                self.pending = self.pending[1:]
            if self.pending == self.secret:
                output.extend(b"[REDACTED]")
                self.pending = b""
        if final:
            output.extend(self.pending)
            self.pending = b""
        return bytes(output)


def _is_docker_command(command: list[str]) -> bool:
    return bool(command) and (
        command[0] == "docker-compose"
        or (len(command) >= 2 and command[0] == "docker" and command[1] == "compose")
    )


def _secure_base_command(exporter_cmd: str) -> list[str]:
    tokens = split_command(exporter_cmd)
    if tokens != _DEFAULT_EXPORTER_TOKENS:
        raise ConfigError(
            "A passphrase requires the supported Docker Compose exporter command. "
            "Custom commands must provide a reviewed stdin-based secure adapter; "
            "the secret will not be placed in argv or an environment value."
        )
    return ["docker", "compose", "exec", "-T", "webserver", "python", "-c", _PASSPHRASE_BRIDGE]


def build_command(
    exporter_cmd: str,
    target: str,
    *,
    filename_format: bool,
    compare_checksums: bool,
    delete: bool,
    secure_passphrase: bool = False,
) -> list[str]:
    command = (
        _secure_base_command(exporter_cmd) if secure_passphrase else split_command(exporter_cmd)
    )
    command = [*command, target]
    if filename_format:
        command.append("--use-filename-format")
    if compare_checksums:
        command.append("--compare-checksums")
    if delete:
        command.append("--delete")
    return command


def _bounded_tail(tail: bytearray, chunk: bytes) -> None:
    tail.extend(chunk)
    if len(tail) > DIAGNOSTIC_TAIL_BYTES:
        del tail[: len(tail) - DIAGNOSTIC_TAIL_BYTES]


def _remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _run(
    command: list[str],
    *,
    stdin_value: str | None = None,
    echo: bool = True,
    timeout_seconds: float = DEFAULT_EXPORT_TIMEOUT_SECONDS,
) -> _Completed:
    """Relay child output without blocking heartbeat, timeout, or cancellation."""
    logger.info("Running: %s", shlex.join(command))
    secret = stdin_value.encode("utf-8") if stdin_value is not None else None
    if secret is not None and len(secret) > MAX_PASSPHRASE_BYTES:
        raise ConfigError(
            f"The passphrase exceeds the supported {MAX_PASSPHRASE_BYTES}-byte bound."
        )
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if secret is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
    except FileNotFoundError as exc:
        raise ServerUnreachableError(
            f"Cannot run the exporter: {exc}. Is Docker (or the webserver container) available? "
            "Override the command with --exporter-cmd if Paperless runs differently."
        ) from exc

    if secret is not None:
        assert process.stdin is not None
        try:
            process.stdin.write(secret + b"\n")
            process.stdin.close()
        except BrokenPipeError:
            # The bounded child diagnostic below explains why it exited early.
            pass

    signals = _Signals()
    redactor = _SecretRedactor(secret)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    tail = bytearray()
    assert process.stdout is not None
    chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=16)

    def read_output() -> None:
        assert process.stdout is not None
        try:
            while chunk := process.stdout.read(STREAM_CHUNK_BYTES):
                chunks.put(chunk)
        finally:
            chunks.put(None)

    reader = threading.Thread(target=read_output, name="paperless-export-output", daemon=True)
    reader.start()
    started = time.monotonic()
    deadline = started + timeout_seconds if timeout_seconds > 0 else None
    last_heartbeat = started
    output_bytes = 0
    finished = False
    try:
        while not finished:
            remaining = _remaining(deadline)
            if remaining is not None and remaining <= 0:
                _stop_child(process, deadline=deadline)
                raise ExporterFailedError(
                    f"document_exporter exceeded the configured {timeout_seconds:g}s timeout; "
                    "the child was terminated and committed Paperless output was left intact.",
                    124,
                )
            try:
                wait_timeout = min(0.25, max(0.01, HEARTBEAT_SECONDS))
                if remaining is not None:
                    wait_timeout = min(wait_timeout, remaining)
                chunk = chunks.get(timeout=wait_timeout)
            except queue.Empty:
                chunk = b""
            if chunk is None:
                finished = True
                continue
            if chunk:
                safe_chunk = redactor.feed(chunk)
                output_bytes += len(safe_chunk)
                signals.observe(safe_chunk)
                _bounded_tail(tail, safe_chunk)
                if echo:
                    text = decoder.decode(safe_chunk)
                    if text:
                        safe_text = sanitize_text(text)
                        sys.stderr.write(safe_text)
                        sys.stderr.flush()
                        transcript_logger.debug(
                            "document_exporter output: %s",
                            safe_text.rstrip(),
                        )
            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_SECONDS:
                elapsed = now - started
                event = snapshot(
                    "exporter",
                    output_bytes,
                    None,
                    0,
                    output_bytes / elapsed if elapsed else 0.0,
                    elapsed,
                    durable=0,
                )
                heartbeat = event.render()
                logger.info("%s", heartbeat)
                if echo:
                    sys.stderr.write(f"{heartbeat}\n")
                    sys.stderr.flush()
                last_heartbeat = now
    except BaseException:
        if process.poll() is None:
            _stop_child(process, deadline=deadline)
        raise
    finally:
        reader_timeout = _remaining(deadline)
        reader_timeout = 1.0 if reader_timeout is None else min(reader_timeout, 1.0)
        reader.join(timeout=reader_timeout)
        process.stdout.close()

    final_chunk = redactor.feed(b"", final=True)
    if final_chunk:
        signals.observe(final_chunk)
        _bounded_tail(tail, final_chunk)
        if echo:
            text = sanitize_text(decoder.decode(final_chunk))
            sys.stderr.write(text)
            transcript_logger.debug("document_exporter output: %s", text.rstrip())
    if echo:
        remainder = decoder.decode(b"", final=True)
        if remainder:
            sys.stderr.write(sanitize_text(remainder))
        sys.stderr.flush()

    try:
        returncode = process.wait(timeout=_remaining(deadline))
    except subprocess.TimeoutExpired:
        _stop_child(process, deadline=deadline)
        raise ExporterFailedError(
            f"document_exporter exceeded the configured {timeout_seconds:g}s timeout; "
            "the child was terminated and committed Paperless output was left intact.",
            124,
        ) from None

    return _Completed(
        returncode=returncode,
        output=bytes(tail).decode("utf-8", errors="replace"),
        path_too_long=signals.path_too_long,
        docker_unavailable=signals.docker_unavailable,
    )


def _stop_child(process: subprocess.Popen[bytes], *, deadline: float | None) -> None:
    """Terminate a child, escalating to kill only when it does not exit."""
    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    wait_timeout = _remaining(deadline)
    wait_timeout = 5.0 if wait_timeout is None else min(wait_timeout, 5.0)
    try:
        process.wait(timeout=wait_timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        # An exhausted operation deadline leaves no scheduling time to reap a
        # killed child. Give the OS one bounded second to report its exit;
        # otherwise report that cleanup could not be confirmed, never success.
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            raise ExporterFailedError(
                "document_exporter did not confirm exit within the 1s cleanup grace period.",
                124,
            ) from None


def _failure(command: list[str], completed: _Completed, *, flat: bool = False) -> NoReturn:
    tail = completed.output.strip() or "(no diagnostic output)"
    if _is_docker_command(command) and completed.docker_unavailable:
        raise ServerUnreachableError(
            "Docker Compose could not reach its project, service, daemon, or target container "
            f"(child exit {completed.returncode}); bounded diagnostic tail "
            f"(at most {DIAGNOSTIC_TAIL_BYTES} bytes):\n{tail}"
        )
    qualifier = " even without --use-filename-format" if flat else ""
    raise ExporterFailedError(
        f"document_exporter failed{qualifier} (child exit {completed.returncode}); "
        f"bounded diagnostic tail (at most {DIAGNOSTIC_TAIL_BYTES} bytes):\n{tail}",
        completed.returncode,
    )


def run_exporter(
    exporter_cmd: str = DEFAULT_EXPORTER_CMD,
    target: str = DEFAULT_TARGET,
    *,
    filename_format: bool = True,
    compare_checksums: bool = True,
    delete: bool = True,
    fallback_on_long_paths: bool = True,
    passphrase: str | None = None,
    timeout_seconds: float = DEFAULT_EXPORT_TIMEOUT_SECONDS,
) -> ExporterRun:
    """Run `document_exporter`; on a path-length failure, retry flat once."""
    command = build_command(
        exporter_cmd,
        target,
        filename_format=filename_format,
        compare_checksums=compare_checksums,
        delete=delete,
        secure_passphrase=passphrase is not None,
    )
    completed = _run(command, stdin_value=passphrase, timeout_seconds=timeout_seconds)
    if completed.returncode == 0:
        return ExporterRun(
            command,
            used_filename_format=filename_format,
            output=completed.output,
        )

    if filename_format and fallback_on_long_paths and completed.path_too_long:
        logger.warning(
            "Exporter failed because a path exceeded the OS limit. Falling back to a flat "
            "export (no --use-filename-format) — the folder layout is lost for this run, but "
            "manifest.json still preserves every tag/type/correspondent. Consider shortening "
            "long document titles."
        )
        flat_command = build_command(
            exporter_cmd,
            target,
            filename_format=False,
            compare_checksums=compare_checksums,
            delete=delete,
            secure_passphrase=passphrase is not None,
        )
        flat = _run(flat_command, stdin_value=passphrase, timeout_seconds=timeout_seconds)
        if flat.returncode == 0:
            return ExporterRun(flat_command, used_filename_format=False, output=flat.output)
        _failure(flat_command, flat, flat=True)

    _failure(command, completed)
