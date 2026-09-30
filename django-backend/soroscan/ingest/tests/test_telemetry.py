"""Tests for OpenTelemetry custom spans across business logic (issue #1296)."""

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from django.test import TestCase

import soroscan.ingest.telemetry as telemetry


class TelemetrySpanTests(TestCase):
    def setUp(self):
        self.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        # set_tracer_provider refuses to override an already-configured provider,
        # so assign the module global directly for test isolation.
        trace._TRACER_PROVIDER = provider
        # Ensure all ingest modules resolve to the test provider. Modules import
        # ``tracer`` at module load, so rebind each reference explicitly.
        test_tracer = trace.get_tracer("soroscan.ingest")
        telemetry.tracer = test_tracer
        import soroscan.ingest.cache_utils as cache_utils
        import soroscan.ingest.consumers as consumers
        import soroscan.ingest.stellar_client as stellar_client
        import soroscan.ingest.tasks as tasks

        cache_utils.tracer = test_tracer
        consumers.tracer = test_tracer
        stellar_client.tracer = test_tracer
        tasks.tracer = test_tracer

    def _span_names(self):
        return {span.name for span in self.exporter.get_finished_spans()}

    def test_span_helper_records_attributes(self):
        with telemetry.span("test.unit", attributes={"foo": "bar"}):
            pass
        spans = self.exporter.get_finished_spans()
        self.assertTrue(
            any(
                span.name == "test.unit" and span.attributes.get("foo") == "bar"
                for span in spans
            )
        )

    def test_configure_tracing_is_noop_without_endpoint(self):
        # Should not raise and should not crash when the optional exporter
        # is absent / unconfigured.
        telemetry.configure_tracing()
        self.assertTrue(True)

    def test_get_cached_contract_emits_contract_and_db_spans(self):
        from soroscan.ingest.cache_utils import get_cached_contract

        get_cached_contract(
            "CDOESNOTEXIST0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"
        )
        names = self._span_names()
        self.assertIn("ingest.contract_lookup", names)
        self.assertIn("db.query.contract_lookup", names)

    def test_get_contract_state_emits_rpc_span(self):
        from soroscan.ingest.stellar_client import SorobanClient

        client = SorobanClient()
        # get_ledger_entries is unavailable on a non-configured server, so the
        # span is still opened even though the RPC call short-circuits.
        client.get_contract_state("CABC123")
        self.assertIn("soroban.rpc.get_contract_state", self._span_names())


class ProcessMemoryGaugeTests(TestCase):
    def test_gauge_reports_current_rss(self):
        from prometheus_client import REGISTRY

        value = REGISTRY.get_sample_value("soroscan_process_resident_memory_bytes")
        self.assertIsNotNone(value)
        self.assertGreater(value, 0)

    def test_gauge_reads_rss_at_scrape_time(self):
        from unittest.mock import patch

        from prometheus_client import REGISTRY

        with patch.object(telemetry._process, "memory_info") as memory_info:
            memory_info.return_value.rss = 12345
            value = REGISTRY.get_sample_value(
                "soroscan_process_resident_memory_bytes"
            )
        self.assertEqual(value, 12345)

    def test_metrics_endpoint_includes_memory_gauge(self):
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "soroscan_process_resident_memory_bytes", response.content.decode()
        )
