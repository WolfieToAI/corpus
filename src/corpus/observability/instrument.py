"""One context manager for a pipeline stage: a span (per-request waterfall in the trace) and a
histogram sample (aggregate latency in dashboards), with the same boundaries."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from corpus.observability.metrics import Timer
from corpus.observability.tracing import tracer


@contextmanager
def stage(span_name: str, histogram: Any, **attributes: str | int | float | bool) -> Iterator[None]:
    with tracer().start_as_current_span(span_name, attributes=attributes), Timer(histogram):
        yield
