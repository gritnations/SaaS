"""In-process metrics, rendered in the Prometheus text exposition format.

The counters live in memory in one process. They reset when the process restarts, and each process of a
multi-process deployment reports its own numbers. No metrics library is used, because three series do
not justify a dependency.
"""

import threading
import time
from collections.abc import Callable


class Metrics:
    """A small, thread-safe set of operational counters.

    Series exposed:
        * ``platform_info``: constant 1, labelled with the API version.
        * ``platform_uptime_seconds``: seconds since this object was created.
        * ``platform_http_requests_total``: responses sent, labelled by status class such as ``2xx``.
    """

    def __init__(self, version: str, clock: Callable[[], float] = time.monotonic) -> None:
        """Start counting from now.

        Args:
            version: The API version reported in ``platform_info``.
            clock: A monotonic clock returning seconds. Replaceable in tests.
        """
        self._version = version
        self._clock = clock
        self._started = clock()
        self._lock = threading.Lock()
        self._responses: dict[str, int] = {}

    def record_response(self, status_code: int) -> None:
        """Count one response.

        Args:
            status_code: The HTTP status code that was sent, for example 200 or 503.
        """
        status_class = f"{status_code // 100}xx"
        with self._lock:
            self._responses[status_class] = self._responses.get(status_class, 0) + 1

    def render(self) -> str:
        """Render every series as Prometheus text.

        Returns:
            The exposition text, ending with a newline.
        """
        with self._lock:
            responses = sorted(self._responses.items())
        lines = [
            "# HELP platform_info Platform build information.",
            "# TYPE platform_info gauge",
            f'platform_info{{version="{self._version}"}} 1',
            "# HELP platform_uptime_seconds Seconds since the API process started.",
            "# TYPE platform_uptime_seconds gauge",
            f"platform_uptime_seconds {self._clock() - self._started:.3f}",
            "# HELP platform_http_requests_total Responses sent, by status class.",
            "# TYPE platform_http_requests_total counter",
        ]
        lines.extend(f'platform_http_requests_total{{status_class="{name}"}} {count}' for name, count in responses)
        return "\n".join(lines) + "\n"
