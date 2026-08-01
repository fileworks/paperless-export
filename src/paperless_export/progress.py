"""Typed progress snapshots shared by exporter and post-processing phases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

ProgressPhase = Literal[
    "exporter",
    "manifest",
    "pdf_metadata",
    "view_preflight",
    "view_materialization",
    "publication",
    "completion",
]
_PHASES = {
    "exporter",
    "manifest",
    "pdf_metadata",
    "view_preflight",
    "view_materialization",
    "publication",
    "completion",
}


@dataclass(frozen=True)
class ProgressEvent:
    """One stable operational snapshot for terminal, logfile, and tests."""

    phase: ProgressPhase
    current: int
    total: int | None
    durable: int
    failures: int
    rate: float
    elapsed: float
    eta_seconds: float | None

    def render(self) -> str:
        """Render a concise line without embedding secret-bearing inputs."""

        total_text = str(self.total) if self.total is not None else "?"
        eta = "" if self.eta_seconds is None else f" eta={self.eta_seconds:.1f}s"
        return (
            f"Progress phase={self.phase} current={self.current} total={total_text} "
            f"durable={self.durable} failures={self.failures} "
            f"rate={self.rate:.1f}/s elapsed={self.elapsed:.1f}s{eta}"
        )


def snapshot(
    phase: str,
    current: int,
    total: int | None,
    failures: int,
    rate: float,
    elapsed: float,
    *,
    durable: int | None = None,
) -> ProgressEvent:
    """Validate legacy callback values and produce the public typed schema."""

    if phase not in _PHASES:
        msg = f"unknown progress phase: {phase}"
        raise ValueError(msg)
    eta = None
    if total is not None and current >= 5 and rate > 0 and current <= total:
        eta = (total - current) / rate
    return ProgressEvent(
        phase=cast(ProgressPhase, phase),
        current=max(0, current),
        total=None if not total else max(0, total),
        durable=max(0, current - failures if durable is None else durable),
        failures=max(0, failures),
        rate=max(0.0, rate),
        elapsed=max(0.0, elapsed),
        eta_seconds=eta,
    )
