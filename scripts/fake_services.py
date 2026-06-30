#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread


class JsonHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json({"status": "ok"})
        elif self.path == "/health":
            self._json({"status": "ok"})
        elif self.path == "/v1/models":
            self._json({"object": "list", "data": [{"id": "fake-model"}]})
        else:
            self._json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)

        if self.path == "/v1/audio/transcriptions":
            self._json({"text": "fake transcript"})
        elif self.path == "/v1/audio/speech":
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.end_headers()
            self.wfile.write(make_silent_wav())
        elif self.path == "/v1/chat/completions":
            self._json({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
        else:
            self._json({"error": "not found"}, status=404)

    def log_message(self, format: str, *args) -> None:
        return

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_silent_wav() -> bytes:
    import io
    import wave

    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(22050)
        wav_file.writeframes(b"\x00\x00" * 256)
    return output.getvalue()


async def tcp_server(host: str, port: int) -> asyncio.AbstractServer:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    return await asyncio.start_server(handle, host, port)


def start_http(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), JsonHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--asr-http-port", type=int, default=8080)
    parser.add_argument("--tts-http-port", type=int, default=8081)
    parser.add_argument("--llm-port", type=int, default=8001)
    parser.add_argument("--wakeword-port", type=int, default=10400)
    args = parser.parse_args()

    http_servers = [
        start_http(args.host, args.asr_http_port),
        start_http(args.host, args.tts_http_port),
        start_http(args.host, args.llm_port),
    ]
    wakeword = await tcp_server(args.host, args.wakeword_port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        wakeword.close()
        await wakeword.wait_closed()
        for server in http_servers:
            server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
