"""OpenTelemetry setup: one trace per Run, spans per stage, plus HTTP and model spans."""

import logging

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SpanExporter


def setup_tracing(exporter: str) -> None:
    """`exporter`: "none", "console" or "otlp" (endpoint from OTEL_EXPORTER_OTLP_ENDPOINT)."""
    if exporter == "none":
        return
    span_exporter: SpanExporter
    if exporter == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        span_exporter = OTLPSpanExporter()
    else:
        span_exporter = ConsoleSpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "clinical-trials-viz"}))
    provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(provider)

    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from pydantic_ai import Agent

    HTTPXClientInstrumentor().instrument()
    Agent.instrument_all()


def configure_logging(level: str) -> None:
    """Send the service's warnings and errors (model failures, source errors, bugs with run IDs) to stderr."""
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
