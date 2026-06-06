"""OpenTelemetry wiring for OTLP HTTP export to OpenObserve.

OpenObserve receives OTLP natively at http://openobserve:5080/api/default/v1/{signal}
with HTTP Basic auth (base64(email:password)). We export traces, logs and metrics.
All setup is best-effort: if the collector is unreachable the agent still runs.
"""
import base64
import logging
import os

from opentelemetry import trace, metrics
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry._logs import set_logger_provider

_SERVICE = "stock-research-agent"
_tracer = trace.get_tracer(_SERVICE)
_meter = None
_counters: dict = {}
# Dedicated stdlib logger bridged to OTel via LoggingHandler. Using the handler
# (rather than hand-constructing LogRecord objects) is the version-stable path
# and automatically correlates each log with the active span.
_otel_logger = logging.getLogger("agent.otel")
_otel_logger.setLevel(logging.INFO)
_otel_logger.propagate = False


def _auth_headers() -> dict:
    email = os.getenv("ZO_ROOT_USER_EMAIL", "admin@poc.local")
    password = os.getenv("ZO_ROOT_USER_PASSWORD", "changeme123")
    token = base64.b64encode(f"{email}:{password}".encode()).decode()
    # `stream-name` groups OTLP logs into a named OpenObserve stream.
    return {"Authorization": f"Basic {token}", "stream-name": "default"}


def setup_telemetry() -> None:
    """Initialise tracer, meter and logger providers pointed at OpenObserve."""
    global _tracer, _meter
    base = os.getenv("OTLP_ENDPOINT", "http://openobserve:5080/api/default").rstrip("/")
    headers = _auth_headers()
    resource = Resource.create({"service.name": _SERVICE})

    # --- Traces ---
    tp = TracerProvider(resource=resource)
    tp.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(endpoint=f"{base}/v1/traces", headers=headers)))
    trace.set_tracer_provider(tp)
    _tracer = trace.get_tracer(_SERVICE)

    # --- Metrics ---
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{base}/v1/metrics", headers=headers),
        export_interval_millis=15000,
    )
    mp = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(mp)
    _meter = metrics.get_meter(_SERVICE)
    _counters["input_tokens"] = _meter.create_counter("agent.api_usage.input_tokens")
    _counters["output_tokens"] = _meter.create_counter("agent.api_usage.output_tokens")
    _counters["cache_read_tokens"] = _meter.create_counter("agent.api_usage.cache_read_tokens")
    _counters["cache_creation_tokens"] = _meter.create_counter("agent.api_usage.cache_creation_tokens")
    _counters["cost_usd"] = _meter.create_counter("agent.api_usage.cost_usd")

    # --- Logs ---
    lp = LoggerProvider(resource=resource)
    lp.add_log_record_processor(BatchLogRecordProcessor(
        OTLPLogExporter(endpoint=f"{base}/v1/logs", headers=headers)))
    set_logger_provider(lp)
    handler = LoggingHandler(level=logging.INFO, logger_provider=lp)
    # Avoid duplicate handlers if setup runs twice.
    if not any(isinstance(h, LoggingHandler) for h in _otel_logger.handlers):
        _otel_logger.addHandler(handler)

    print(f"[otel] exporting to {base}", flush=True)


def get_tracer():
    return _tracer


def log_event(event_name: str, body: str, attributes: dict | None = None) -> None:
    """Emit a structured OTel log record (agent.message / thinking / portfolio.snapshot).

    The LoggingHandler turns the `extra` dict into log-record attributes and
    attaches the active span context automatically.
    """
    try:
        attrs = {"event.name": event_name}
        for k, v in (attributes or {}).items():
            # Coerce non-scalar attribute values to strings for OTLP.
            attrs[k] = v if isinstance(v, (str, int, float, bool)) else str(v)
        _otel_logger.info(body, extra=attrs)
    except Exception as e:  # noqa: BLE001
        print(f"[otel] log_event failed: {e}", flush=True)


def record_usage(input_tokens: int, output_tokens: int, cost_usd: float,
                 cache_read: int = 0, cache_creation: int = 0) -> None:
    try:
        if _counters:
            _counters["input_tokens"].add(input_tokens)
            _counters["output_tokens"].add(output_tokens)
            _counters["cache_read_tokens"].add(cache_read)
            _counters["cache_creation_tokens"].add(cache_creation)
            _counters["cost_usd"].add(cost_usd)
    except Exception as e:  # noqa: BLE001
        print(f"[otel] record_usage failed: {e}", flush=True)
