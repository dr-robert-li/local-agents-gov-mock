"""OpenTelemetry wiring for the emulator: FastAPI request spans + structured
logs exported over OTLP HTTP to OpenObserve (HTTP Basic auth). Best-effort —
if the collector is unreachable the service still serves requests.
"""
import base64
import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry._logs import set_logger_provider
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

_log = logging.getLogger("emulator.otel")
_log.setLevel(logging.INFO)
_log.propagate = False


def _auth_headers() -> dict:
    email = os.getenv("ZO_ROOT_USER_EMAIL", "admin@poc.local")
    pw = os.getenv("ZO_ROOT_USER_PASSWORD", "")
    token = base64.b64encode(f"{email}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}", "stream-name": "default"}


def setup(app, service_name: str) -> None:
    base = os.getenv("OTLP_ENDPOINT", "http://openobserve:5080/api/default").rstrip("/")
    headers = _auth_headers()
    resource = Resource.create({"service.name": service_name})

    tp = TracerProvider(resource=resource)
    tp.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(endpoint=f"{base}/v1/traces", headers=headers)))
    trace.set_tracer_provider(tp)

    lp = LoggerProvider(resource=resource)
    lp.add_log_record_processor(BatchLogRecordProcessor(
        OTLPLogExporter(endpoint=f"{base}/v1/logs", headers=headers)))
    set_logger_provider(lp)
    if not any(isinstance(h, LoggingHandler) for h in _log.handlers):
        _log.addHandler(LoggingHandler(level=logging.INFO, logger_provider=lp))

    # Auto-trace every HTTP request as a server span.
    FastAPIInstrumentor.instrument_app(app, tracer_provider=tp)
    print(f"[otel] {service_name} exporting to {base}", flush=True)


def emit(event_name: str, body: str, attributes: dict | None = None) -> None:
    try:
        attrs = {"event.name": event_name}
        for k, v in (attributes or {}).items():
            attrs[k] = v if isinstance(v, (str, int, float, bool)) else str(v)
        _log.info(body, extra=attrs)
    except Exception as e:  # noqa: BLE001
        print(f"[otel] emit failed: {e}", flush=True)
