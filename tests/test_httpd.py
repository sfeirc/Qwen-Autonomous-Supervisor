from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from conftest import write_config

from qas.config import load_config
from qas.httpd import _loopback, serve
from qas.runtime import Supervisor


def test_loopback_detection() -> None:
    assert _loopback("localhost")
    assert _loopback("127.0.0.1")
    assert not _loopback("0.0.0.0")  # noqa: S104 - deliberate unsafe-bind test
    assert not _loopback("not-an-address")


def test_remote_binding_requires_token(
    tmp_path: Path, git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path, git_project)
    path.write_text(
        path.read_text(encoding="utf-8") + "observability:\n  host: 0.0.0.0\n  port: 8787\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("QAS_OBSERVABILITY_TOKEN", raising=False)
    supervisor = Supervisor(load_config(path), Path(__file__).parents[1])
    with pytest.raises(ValueError, match="TOKEN"):
        serve(supervisor)


def test_local_health_status_metrics(
    tmp_path: Path, git_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import qas.httpd as httpd

    real_server = httpd.ThreadingHTTPServer
    holder: list[httpd.ThreadingHTTPServer] = []
    ready = threading.Event()

    def factory(address: tuple[str, int], handler: type[httpd.BaseHTTPRequestHandler]) -> Any:
        server = real_server(address, handler)
        holder.append(server)
        ready.set()
        return server

    monkeypatch.setattr(httpd, "ThreadingHTTPServer", factory)
    path = write_config(tmp_path, git_project)
    path.write_text(
        path.read_text(encoding="utf-8") + "observability:\n  host: 127.0.0.1\n  port: 0\n",
        encoding="utf-8",
    )
    # Port zero is valid at the socket layer but rejected in config, so reserve one first.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    path.write_text(path.read_text().replace("port: 0", f"port: {port}"), encoding="utf-8")
    supervisor = Supervisor(load_config(path), Path(__file__).parents[1])
    thread = threading.Thread(target=serve, args=(supervisor,), daemon=True)
    thread.start()
    assert ready.wait(3)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
            assert json.loads(response.read())["healthy"]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=3) as response:
            assert response.status == 200
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=3) as response:
            assert b"qas_healthy 1" in response.read()
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/missing", timeout=3)
        assert exc.value.code == 404
    finally:
        holder[0].shutdown()
        thread.join(timeout=3)
