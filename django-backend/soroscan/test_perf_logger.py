import asyncio
import json
import os
from unittest.mock import patch

from django.test import TestCase, override_settings, RequestFactory
from django.db import connection
from django.http import HttpResponse

from soroscan.perf_logger import SlowQueryLoggerMiddleware


class MockTimeSlow:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        # First call (start) returns 0.0, second call (end) returns 1.1. Duration = 1.1s
        return 0.0 if self.calls % 2 != 0 else 1.1


class MockTimeFast:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        # First call (start) returns 0.0, second call (end) returns 0.9. Duration = 0.9s
        return 0.0 if self.calls % 2 != 0 else 0.9


async def _query_response(request):
    # The middleware is async (c27dd70b), so get_response must be awaitable.
    # The query runs on this thread so the middleware's execute_wrapper sees it.
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return HttpResponse("OK")


async def _plain_response(request):
    return HttpResponse("OK")


@patch.dict(os.environ, {"DJANGO_ALLOW_ASYNC_UNSAFE": "true"})
class SlowQueryLoggerMiddlewareTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _call(self, middleware, request):
        """Run the async middleware from a synchronous test."""
        return asyncio.run(middleware(request))

    @override_settings(DATABASE_SLOW_QUERY_THRESHOLD=1.0)
    def test_slow_query_logged(self):
        middleware = SlowQueryLoggerMiddleware(_query_response)
        request = self.factory.get("/")

        with patch("soroscan.perf_logger.monotonic", side_effect=MockTimeSlow()):
            with self.assertLogs("django.performance.database", level="WARNING") as cm:
                self._call(middleware, request)

        self.assertTrue(any("Slow query detected: SQL" in log for log in cm.output))
        self.assertTrue(any("1.1000s" in log for log in cm.output))

    @override_settings(DATABASE_SLOW_QUERY_THRESHOLD=1.0)
    def test_fast_query_not_logged(self):
        middleware = SlowQueryLoggerMiddleware(_query_response)
        request = self.factory.get("/")

        with patch("soroscan.perf_logger.monotonic", side_effect=MockTimeFast()):
            with self.assertNoLogs("django.performance.database", level="WARNING"):
                self._call(middleware, request)


@patch.dict(os.environ, {"DJANGO_ALLOW_ASYNC_UNSAFE": "true"})
@override_settings(
    DATABASE_SLOW_QUERY_THRESHOLD=10.0, REQUEST_LATENCY_LOG_THRESHOLD_MS=100
)
class RequestLatencyLoggingTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _call(self, middleware, request):
        return asyncio.run(middleware(request))

    def _latency_entries(self, cm):
        return [json.loads(record.getMessage()) for record in cm.records]

    def test_slow_request_logs_json_breakdown(self):
        middleware = SlowQueryLoggerMiddleware(_query_response)
        request = self.factory.get("/api/contracts/")

        # Request takes 250ms in total; the single query takes 40ms of it.
        with patch(
            "soroscan.perf_logger.perf_counter", side_effect=[0.0, 0.25]
        ), patch(
            "soroscan.perf_logger.monotonic", side_effect=[1.0, 1.04]
        ):
            with self.assertLogs("django.performance.request", level="WARNING") as cm:
                self._call(middleware, request)

        [entry] = self._latency_entries(cm)
        self.assertEqual(entry["event"], "request_latency")
        self.assertEqual(entry["method"], "GET")
        self.assertEqual(entry["path"], "/api/contracts/")
        self.assertEqual(entry["status_code"], 200)
        self.assertEqual(entry["total_ms"], 250.0)
        self.assertEqual(entry["db_time_ms"], 40.0)
        self.assertEqual(entry["cpu_time_ms"], 210.0)

    def test_request_without_queries_reports_zero_db_time(self):
        middleware = SlowQueryLoggerMiddleware(_plain_response)
        request = self.factory.get("/")

        with patch("soroscan.perf_logger.perf_counter", side_effect=[0.0, 0.15]):
            with self.assertLogs("django.performance.request", level="WARNING") as cm:
                self._call(middleware, request)

        [entry] = self._latency_entries(cm)
        self.assertEqual(entry["db_time_ms"], 0.0)
        self.assertEqual(entry["cpu_time_ms"], 150.0)

    def test_fast_request_is_not_logged(self):
        middleware = SlowQueryLoggerMiddleware(_plain_response)
        request = self.factory.get("/")

        with patch("soroscan.perf_logger.perf_counter", side_effect=[0.0, 0.1]):
            with self.assertNoLogs("django.performance.request", level="WARNING"):
                self._call(middleware, request)

    @override_settings(REQUEST_LATENCY_LOG_THRESHOLD_MS=500)
    def test_threshold_is_configurable(self):
        middleware = SlowQueryLoggerMiddleware(_plain_response)
        request = self.factory.get("/")

        with patch("soroscan.perf_logger.perf_counter", side_effect=[0.0, 0.3]):
            with self.assertNoLogs("django.performance.request", level="WARNING"):
                self._call(middleware, request)
