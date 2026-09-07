"""Cross-domain observability contracts."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

REQUESTED_TRACE_SAMPLING_RATE = 1.0


@dataclass(frozen=True)
class RequestedTraceContext:
    trace_id: str
    span_id: str
    requested_sampling: float

    @property
    def header(self) -> str:
        return (
            f"Root={self.trace_id};Parent={self.span_id};"
            f"Sampled={int(self.requested_sampling)}"
        )


def requested_xray_trace_context(
    *,
    now: Callable[[], float] = time.time,
    random_hex: Callable[[], str] = lambda: uuid.uuid4().hex,
) -> RequestedTraceContext:
    """Create the trace context requested from AgentCore, not an observation."""
    epoch_hex = f"{int(now()):08x}"[-8:]
    return RequestedTraceContext(
        trace_id=f"1-{epoch_hex}-{random_hex()[:24]}",
        span_id=random_hex()[:16],
        requested_sampling=REQUESTED_TRACE_SAMPLING_RATE,
    )
