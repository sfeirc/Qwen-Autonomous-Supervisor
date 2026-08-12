from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from typing import Any

from qas.runtime import Supervisor


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def serve(supervisor: Supervisor) -> None:
    host = supervisor.config.observability.host
    token = os.environ.get("QAS_OBSERVABILITY_TOKEN")
    if not _loopback(host) and not token:
        raise ValueError(
            "QAS_OBSERVABILITY_TOKEN is required for a non-loopback observability host"
        )

    class Handler(BaseHTTPRequestHandler):
        server_version = "QAS/0.1"

        def _authorized(self) -> bool:
            if not token:
                return True
            supplied = self.headers.get("Authorization", "")
            return hmac.compare_digest(supplied, f"Bearer {token}")

        def _send(self, code: int, content_type: str, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                self._send(401, "application/json", '{"error":"unauthorized"}')
                return
            status = supervisor.status()
            if self.path == "/healthz":
                code = 200 if status["healthy"] else 503
                self._send(code, "application/json", json.dumps({"healthy": status["healthy"]}))
            elif self.path == "/status":
                self._send(
                    200, "application/json", json.dumps(status, ensure_ascii=False, default=str)
                )
            elif self.path == "/metrics":
                counts = status["run_counts"]
                lines = [
                    "# TYPE qas_runs_total gauge",
                    *(
                        f'qas_runs_total{{status="{key}"}} {value}'
                        for key, value in sorted(counts.items())
                    ),
                    "# TYPE qas_quarantined_failures gauge",
                    f"qas_quarantined_failures {status['quarantined_failures']}",
                    "# TYPE qas_healthy gauge",
                    f"qas_healthy {1 if status['healthy'] else 0}",
                    "# TYPE qas_uptime_seconds gauge",
                    f"qas_uptime_seconds {status['uptime_seconds']:.3f}",
                    "# TYPE qas_model_calls_total counter",
                    f"qas_model_calls_total {status['usage']['model_calls']}",
                    "# TYPE qas_tokens_total counter",
                    f"qas_tokens_total {status['usage']['total_tokens']}",
                    "# TYPE qas_estimated_cost gauge",
                    f"qas_estimated_cost {status['estimated_cost']:.8f}",
                    "# TYPE qas_ticks_successful_total counter",
                    f"qas_ticks_successful_total {status['delivery']['ticks_successful']}",
                    "# TYPE qas_ticks_failed_total counter",
                    f"qas_ticks_failed_total {status['delivery']['ticks_failed']}",
                    "# TYPE qas_recovered_runs_total counter",
                    f"qas_recovered_runs_total {status['delivery']['recovered_runs']}",
                ]
                self._send(200, "text/plain; version=0.0.4", "\n".join(lines) + "\n")
            else:
                self._send(404, "application/json", '{"error":"not_found"}')

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, supervisor.config.observability.port), Handler)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
