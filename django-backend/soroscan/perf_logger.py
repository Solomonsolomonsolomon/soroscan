import json
import logging
from contextlib import ExitStack
from time import monotonic, perf_counter

from django.conf import settings
from django.db import connections
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger("django.performance.database")
latency_logger = logging.getLogger("django.performance.request")

DEFAULT_LATENCY_THRESHOLD_MS = 100


class SlowQueryLoggerMiddleware(MiddlewareMixin):
    """
    Middleware that monitors and logs database queries exceeding a configurable execution time threshold.
    Uses django.db.connection.execute_wrapper for accurate timing and minimal overhead.

    It also sums the time each request spends in the database. When a request
    takes longer than ``REQUEST_LATENCY_LOG_THRESHOLD_MS`` (default 100ms), a
    structured JSON entry splits the total into ``db_time_ms`` and
    ``cpu_time_ms`` (the rest of the request, spent in Python).
    """

    async def __call__(self, request):
        # Dynamically read threshold, defaulting to 1.0 seconds
        threshold = getattr(settings, "DATABASE_SLOW_QUERY_THRESHOLD", 1.0)
        db_time = [0.0]
        request_start = perf_counter()

        with ExitStack() as stack:
            # We apply the execute_wrapper to all configured databases to support multi-DB setups
            for alias in connections:
                # We need to capture the current alias inside the loop securely.
                def make_wrapper(db_alias):
                    def _execute_wrapper(execute, sql, params, many, context):
                        start_time = monotonic()
                        try:
                            return execute(sql, params, many, context)
                        finally:
                            duration = monotonic() - start_time
                            db_time[0] += duration
                            if duration > threshold:
                                safe_params = str(params)[:1000] if params else ""
                                try:
                                    logger.warning(
                                        "Slow query detected: SQL [%s] Execution Time: [%.4fs] DB Alias: [%s]",
                                        sql,
                                        duration,
                                        db_alias,
                                        extra={
                                            "duration": round(duration, 4),
                                            "sql": sql,
                                            "params": safe_params,
                                            "db_alias": db_alias,
                                        },
                                    )
                                except Exception:
                                    # Never fail the user request due to logging exceptions
                                    pass
                    return _execute_wrapper

                stack.enter_context(connections[alias].execute_wrapper(make_wrapper(alias)))

            response = await self.get_response(request)

        total_ms = (perf_counter() - request_start) * 1000
        self._log_latency(request, response, total_ms, db_time[0] * 1000)
        return response

    @staticmethod
    def _log_latency(request, response, total_ms, db_time_ms):
        threshold_ms = getattr(
            settings, "REQUEST_LATENCY_LOG_THRESHOLD_MS", DEFAULT_LATENCY_THRESHOLD_MS
        )
        if total_ms <= threshold_ms:
            return

        entry = {
            "event": "request_latency",
            "method": request.method,
            "path": request.path,
            "status_code": getattr(response, "status_code", None),
            "total_ms": round(total_ms, 2),
            "db_time_ms": round(db_time_ms, 2),
            "cpu_time_ms": round(max(0.0, total_ms - db_time_ms), 2),
        }
        try:
            latency_logger.warning(json.dumps(entry), extra=entry)
        except Exception:
            # Never fail the user request due to logging exceptions
            pass
