from __future__ import annotations

import contextlib
import http.server
import json
import socket
import socketserver
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass

from scripts import smoke_check


class JsonHandler(http.server.BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, dict]] = {}

    def do_GET(self) -> None:
        status, payload = self.routes.get(self.path, (404, {"error": "not found"}))
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


@dataclass
class Server:
    host: str
    port: int


@contextlib.contextmanager
def http_server(routes: dict[str, tuple[int, dict]]) -> Iterator[Server]:
    handler = type("Handler", (JsonHandler,), {"routes": routes})
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield Server("127.0.0.1", server.server_address[1])
        finally:
            server.shutdown()
            thread.join(timeout=2)


@contextlib.contextmanager
def tcp_server() -> Iterator[Server]:
    stop = threading.Event()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    sock.settimeout(0.1)
    port = sock.getsockname()[1]

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with conn:
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield Server("127.0.0.1", port)
    finally:
        stop.set()
        sock.close()
        thread.join(timeout=2)


def test_check_http_any_uses_first_successful_endpoint() -> None:
    with http_server({"/health": (404, {}), "/v1/models": (200, {"object": "list"})}) as server:
        result = smoke_check.check_http_any(
            "llm-http",
            [
                f"http://{server.host}:{server.port}/health",
                f"http://{server.host}:{server.port}/v1/models",
            ],
            timeout=1,
        )

    assert result.ok
    assert "/v1/models" in result.detail


def test_check_tcp_reports_reachable_port() -> None:
    with tcp_server() as server:
        result = smoke_check.check_tcp("asr-wyoming", server.host, server.port, timeout=1)

    assert result.ok
    assert "reachable" in result.detail


def test_main_returns_nonzero_when_required_check_fails() -> None:
    assert smoke_check.main(["--host", "127.0.0.1", "--timeout", "0.01"]) == 1


def test_compose_builds_openwakeword_locally() -> None:
    result = subprocess.run(
        ["docker-compose", "config"],
        check=True,
        text=True,
        capture_output=True,
    )

    assert "dockerfile: wakeword/Dockerfile" in result.stdout
    assert "rhasspy/wyoming-openwakeword" not in result.stdout
