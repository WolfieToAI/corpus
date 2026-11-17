"""OpenTelemetry tracing.

Tracing is off unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set; the API then returns no-op spans
and the instrumentation costs nothing. When on, one trace follows a document from the upload
request, through the queue, into the worker that indexes it: the W3C ``traceparent`` of the
enqueuing span is stored with the job and the worker continues the trace from it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.context import Context

logger = logging.getLogger(__name__)

TRACE_CARRIER_KEY = "_trace"


def tracer() -> trace.Tracer:
    return trace.get_tracer("corpus")


def configure_tracing(endpoint: str | None, service_name: str) -> bool:
    """Install a global tracer provider exporting OTLP/HTTP batches. Returns ``True`` if on."""
    if not endpoint:
        return False
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from corpus import __version__

    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name, "service.version": __version__})
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
    )
    trace.set_tracer_provider(provider)
    # Outgoing HTTP calls (LLM, Voyage) become child spans with their latency and status.
    HTTPXClientInstrumentor().instrument()
    logger.info("tracing enabled: exporting to %s as %s", endpoint, service_name)
    return True


def instrument_engine(engine: Any) -> None:
    """Child span per SQL statement (only meaningful when tracing is configured)."""
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)


def inject_context() -> dict[str, str]:
    """Serialise the current trace context (``traceparent``) to store alongside a job."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def extract_context(payload: Mapping[str, Any]) -> Context:
    carrier = payload.get(TRACE_CARRIER_KEY)
    return propagate.extract(carrier if isinstance(carrier, dict) else {})


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None
